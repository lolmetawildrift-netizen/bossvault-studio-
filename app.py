import os
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

import boto3
import requests
import imageio_ffmpeg
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="BOSSVault Studio", version="0.1.0")

def env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value

def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )

def ffmpeg(*args: str):
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run([exe, "-y", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout)[-5000:])

class ProcessRequest(BaseModel):
    video_key: str = Field(..., min_length=1, description="R2 object key of the raw gameplay MP4")
    output_key: str = Field(..., min_length=1, description="R2 object key for the rendered MP4")
    narration_text: str = Field(..., min_length=1, max_length=1800)
    intro_key: Optional[str] = None
    outro_key: Optional[str] = None
    watermark_key: Optional[str] = None
    game_audio_volume: float = Field(default=0.85, ge=0.0, le=1.0)
    narration_volume: float = Field(default=1.0, ge=0.1, le=2.0)

@app.get("/")
def home():
    return {"service": "BOSSVault Studio", "status": "online", "version": "0.1.0"}

@app.get("/health")
def health():
    return {"status": "online", "service": "BOSSVault Studio"}

@app.post("/process")
def process_video(body: ProcessRequest, x_bossvault_api_key: str = Header(default="")):
    expected = os.getenv("BOSSVault_API_KEY", "")
    if not expected or x_bossvault_api_key != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        s3 = s3_client()
        bucket = env("R2_BUCKET_NAME")
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            raw = work / "gameplay.mp4"
            voice = work / "narration.mp3"
            mixed = work / "mixed.mp4"
            final = work / "final.mp4"

            s3.download_file(bucket, body.video_key, str(raw))

            # Existing Hades Worker endpoint: POST JSON {text}, authenticated by X-BOSSVault-Token.
            tts = requests.post(
                env("HADES_TTS_URL"),
                json={"text": body.narration_text},
                headers={"X-BOSSVault-Token": env("HADES_TTS_TOKEN")},
                timeout=180,
            )
            if not tts.ok:
                raise RuntimeError(f"Hades TTS failed ({tts.status_code}): {tts.text[:1000]}")
            voice.write_bytes(tts.content)

            # Keep the original gameplay duration and game sound. Mix narration over it.
            ffmpeg(
                "-i", str(raw), "-i", str(voice),
                "-filter_complex",
                f"[0:a]volume={body.game_audio_volume}[game];"
                f"[1:a]volume={body.narration_volume}[voice];"
                f"[game][voice]amix=inputs=2:duration=first:dropout_transition=2[a]",
                "-map", "0:v:0", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(mixed)
            )

            # Optional approved animated intro/outro assets surround the complete fight.
            segments = []
            if body.intro_key:
                intro = work / "intro.mp4"
                s3.download_file(bucket, body.intro_key, str(intro))
                segments.append(intro)
            segments.append(mixed)
            if body.outro_key:
                outro = work / "outro.mp4"
                s3.download_file(bucket, body.outro_key, str(outro))
                segments.append(outro)

            if segments:
                # Re-encode segments to a consistent delivery format before concatenation.
                normalized = []
                for i, seg in enumerate(segments):
                    out = work / f"segment_{i}.mp4"
                    ffmpeg(
                        "-i", str(seg if seg.name != "mixed.mp4" else mixed),
                        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                        "-c:a", "aac", "-ar", "48000", "-ac", "2", str(out)
                    )
                    normalized.append(out)
                concat_file = work / "concat.txt"
                concat_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in normalized))
                ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat_file),
                       "-c", "copy", "-movflags", "+faststart", str(final))
            else:
                final = mixed

            s3.upload_file(str(final), bucket, body.output_key,
                           ExtraArgs={"ContentType": "video/mp4"})
            return {"status": "completed", "output_key": body.output_key, "bucket": bucket}

    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing failed: {type(e).__name__}: {e}")

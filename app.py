import os
import tempfile
import subprocess
import uuid
from pathlib import Path
from typing import Optional

import boto3
import requests
import imageio_ffmpeg
from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="BOSSVault Studio", version="0.2.0")


def env(name):
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


def ffmpeg(*args):
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [exe, "-y", *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout)[-4000:])


class ProcessRequest(BaseModel):
    video_key: str = Field(..., min_length=1)
    output_key: str = Field(..., min_length=1)
    narration_text: str = Field(..., min_length=1, max_length=1800)
    intro_key: Optional[str] = None
    outro_key: Optional[str] = None
    watermark_key: Optional[str] = None
    game_audio_volume: float = Field(default=0.85, ge=0, le=1)
    narration_volume: float = Field(default=1.0, ge=0.1, le=2)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOSSVault Studio</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#101014;color:#f4f4f5;font:16px Arial}
main{max-width:720px;margin:28px auto;padding:22px}
header{border-bottom:1px solid #38383f;padding-bottom:18px;margin-bottom:24px}
h1{margin:0;color:#fff;letter-spacing:1px}
h1 span{color:#e04444}
.sub{color:#aaa;margin-top:8px}
.card{background:#1b1b22;border:1px solid #38383f;border-radius:14px;padding:20px}
label{display:block;font-weight:bold;margin:18px 0 8px}
input,textarea{width:100%;padding:13px;border-radius:8px;border:1px solid #555;background:#111;color:#fff}
textarea{min-height:150px;resize:vertical}
button{margin-top:20px;width:100%;padding:15px;border:0;border-radius:9px;background:#b92732;color:white;font-size:17px;font-weight:bold}
.note{color:#aaa;font-size:13px;line-height:1.5}
</style>
</head>
<body>
<main>
<header>
<h1>BOSS<span>VAULT</span> STUDIO</h1>
<div class="sub">Boss fight video processing</div>
</header>
<div class="card">
<form action="/studio/process" method="post" enctype="multipart/form-data">
<label>1. Upload gameplay MP4</label>
<input type="file" name="video" accept="video/mp4,video/*" required>
<label>2. English Hades narration text</label>
<textarea name="narration_text" maxlength="1800" required
placeholder="Write the boss introduction, lore, powers and tactics..."></textarea>
<label>3. Output filename</label>
<input type="text" name="output_name" value="boss-fight-final.mp4" required>
<p class="note">Original gameplay video and sound are retained. Hades narration is mixed with game audio. Keep this page open during processing.</p>
<button type="submit">PROCESS VIDEO</button>
</form>
</div>
<p class="note">BOSSVault Studio</p>
</main>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE


@app.get("/health")
def health():
    return {"status": "online", "service": "BOSSVault Studio"}


def render_video(body):
    s3 = s3_client()
    bucket = env("R2_BUCKET_NAME")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        raw = work / "gameplay.mp4"
        voice = work / "narration.mp3"
        mixed = work / "mixed.mp4"
        final = work / "final.mp4"

        s3.download_file(bucket, body.video_key, str(raw))

        tts = requests.post(
            env("HADES_TTS_URL"),
            json={"text": body.narration_text},
            headers={"X-BOSSVault-Token": env("HADES_TTS_TOKEN")},
            timeout=180,
        )

        if not tts.ok:
            raise RuntimeError(
                f"Hades TTS failed ({tts.status_code}): {tts.text[:800]}"
            )

        voice.write_bytes(tts.content)

        probe = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(raw)],
            capture_output=True,
            text=True,
        )
        has_audio = "Audio:" in (probe.stderr or "")

        if has_audio:
            audio_filter = (
                f"[0:a]volume={body.game_audio_volume}[game];"
                f"[1:a]volume={body.narration_volume}[voice];"
                "[game][voice]amix=inputs=2:duration=first:dropout_transition=2[a]"
            )
            ffmpeg(
                "-i", str(raw), "-i", str(voice),
                "-filter_complex", audio_filter,
                "-map", "0:v:0", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac",
                "-b:a", "192k", "-movflags", "+faststart",
                str(mixed),
            )
        else:
            ffmpeg(
                "-i", str(raw), "-i", str(voice),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac",
                "-b:a", "192k", "-shortest",
                "-movflags", "+faststart", str(mixed),
            )

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

        normalized = []

        for i, seg in enumerate(segments):
            out = work / f"segment_{i}.mp4"
            ffmpeg(
                "-i", str(seg),
                "-vf",
                "scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "23", "-c:a", "aac",
                "-ar", "48000", "-ac", "2", str(out),
            )
            normalized.append(out)

        concat_file = work / "concat.txt"
        concat_file.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in normalized)
        )

        ffmpeg(
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy", "-movflags", "+faststart",
            str(final),
        )

        s3.upload_file(
            str(final), bucket, body.output_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )

        return {
            "status": "completed",
            "output_key": body.output_key,
            "bucket": bucket,
        }


@app.post("/studio/process", response_class=HTMLResponse)
async def studio_process(
    video: UploadFile = File(...),
    narration_text: str = Form(...),
    output_name: str = Form("boss-fight-final.mp4"),
):
    if not video.filename or not video.filename.lower().endswith(
        (".mp4", ".mov", ".m4v")
    ):
        raise HTTPException(status_code=400, detail="Please upload an MP4/MOV video.")

    if not narration_text.strip() or len(narration_text) > 1800:
        raise HTTPException(status_code=400, detail="Narration must be 1–1800 characters.")

    safe_name = Path(output_name).name.strip().replace(" ", "-")
    if not safe_name:
        safe_name = "boss-fight-final.mp4"
    if not safe_name.lower().endswith(".mp4"):
        safe_name += ".mp4"

    try:
        s3 = s3_client()
        bucket = env("R2_BUCKET_NAME")
        video_key = f"uploads/{uuid.uuid4().hex}-{Path(video.filename).name}"

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp:
            temp_path = Path(temp.name)
            temp.write(await video.read())

        try:
            s3.upload_file(
                str(temp_path), bucket, video_key,
                ExtraArgs={"ContentType": "video/mp4"},
            )
        finally:
            temp_path.unlink(missing_ok=True)

        result = render_video(
            ProcessRequest(
                video_key=video_key,
                output_key=f"processed/{safe_name}",
                narration_text=narration_text.strip(),
            )
        )

        return HTMLResponse(
            "<html><body style='background:#101014;color:#fff;font:18px Arial;padding:28px'>"
            "<h2 style='color:#58d68d'>Processing completed</h2>"
            f"<p>Output saved in R2 bucket: <b>{result['bucket']}</b></p>"
            f"<p>File: <b>{result['output_key']}</b></p>"
            "<p>Open Cloudflare R2 to download the processed MP4.</p>"
            "<p><a style='color:#f77' href='/'>Back to Studio</a></p>"
            "</body></html>"
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Processing failed: {type(e).__name__}: {e}",
        )


@app.post("/process")
def process_video(
    body: ProcessRequest,
    x_bossvault_api_key: str = Header(default=""),
):
    expected = os.getenv("BOSSVault_API_KEY", "")

    if not expected or x_bossvault_api_key != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        return render_video(body)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Processing failed: {type(e).__name__}: {e}",
        )

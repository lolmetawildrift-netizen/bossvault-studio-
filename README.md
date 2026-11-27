# BOSSVault Studio

Minimal cloud video-processing API for the BOSSVault workflow.

## Render settings

- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Health check path: `/health`

## Required Render environment variables

- `BOSSVault_API_KEY` — create a long random secret for calls to `/process`.
- `R2_ACCOUNT_ID` — Cloudflare account ID.
- `R2_ACCESS_KEY_ID` — R2 S3 access key ID.
- `R2_SECRET_ACCESS_KEY` — R2 S3 secret access key.
- `R2_BUCKET_NAME` — `bossvault-videos`.
- `HADES_TTS_URL` — existing Hades Worker URL.
- `HADES_TTS_TOKEN` — the Hades Worker token. Never commit this value to GitHub.

## Request

POST `/process`, header `X-BOSSVault-Api-Key: <BOSSVault_API_KEY>`

```json
{
  "video_key": "raw/God-of-War-Daudi.mp4",
  "output_key": "renders/Daudi-Kaupmaor.mp4",
  "narration_text": "Your original English narration...",
  "intro_key": "assets/intro.mp4",
  "outro_key": "assets/outro.mp4"
}
```

`intro_key` and `outro_key` are optional MP4 objects in the same private R2 bucket. Use genuinely animated, approved BOSSVault intro/outro clips here. The API keeps the gameplay duration and mixes narration with the original game audio. It does not yet perform automatic boss-scene detection, generate editorial analysis, or create thumbnails/SEO metadata.

## Important

This is an initial processing service, not a complete one-click publishing system. Do not expose the R2 bucket publicly. Use private R2 credentials and keep all secrets in Render Environment Variables only.

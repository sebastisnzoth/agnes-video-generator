# Deployment Guide

Where each part of Agnes Video Generator runs, why, and the limits you should
expect from each option.

## The one constraint that decides everything

Video generation is a **long-running job**. A single task does, in order:
screenwriting → text-to-speech → reference/storyboard images → video clips →
FFmpeg concat + audio overlay + subtitle burn-in. Even a short 5-second clip
takes minutes end-to-end; a multi-scene video takes tens of minutes.

That rules out serverless for the **worker**, and it is not a timeout-tuning
problem:

| Limit | Why it breaks video generation |
| --- | --- |
| Vercel function duration (60s Hobby / 300s Pro) | A generation task outlives the request that created it. `vercel.json` raises `maxDuration` to 300 but that is still far below real runtime. |
| Instance freezes after the response | `launch_background_task()` uses `asyncio.create_task()`. Once the platform freezes or recycles the instance, that coroutine is gone — silently. |
| Ephemeral, per-instance `/tmp` | On Vercel `core/config.py` points the working dir at `/tmp/agnes-working`. Task state is written to disk there, but a different instance won't see it, so polling `/api/tasks/{id}` hits an empty store. |
| 4.5 MB request body | Song uploads are capped server-side to stay under this (see `effective_max_song_bytes()`). |

So: **Vercel is fine for short stateless API calls, not for the worker.**

## Recommended layout

```
GitHub Pages  ──►  static Vue build (frontend, free, CDN-backed)
       │
       │  fetch('/api/...')  rewritten to VITE_AGNES_BACKEND_URL
       ▼
Persistent host  ──►  FastAPI + FFmpeg worker (Render / Koyeb / Hugging Face Spaces)
       │
       ▼
Agnes AI API  (https://apihub.agnes-ai.com/v1)
```

## Option A — Render (free tier, one click)

`render.yaml` in the repo root is a Render Blueprint.

1. Render dashboard → **Blueprints → New Blueprint Instance** → pick this repo.
2. Render asks for the `sync: false` variables. Put your API key in
   `AGNES_API_KEY`. It is stored as an encrypted env var, never in git.
3. Deploy. Note the service URL, e.g. `https://agnes-video-generator.onrender.com`.

Real limits of the free tier:

- Sleeps after ~15 minutes without a request; cold start ≈ 50s. Keeping the UI
  open polls the backend, which keeps the instance awake during a job.
- No persistent disk: a redeploy wipes `/app/.working_dir`, so old tasks and
  their videos disappear. Export anything you want to keep.
- 512 MB RAM — long compositions can OOM. Move to Starter for real use.

## Option B — Hugging Face Spaces (Docker, more headroom)

Free CPU Spaces give 2 vCPU / 16 GB, which is a much better fit for FFmpeg.

1. New Space → **Docker** SDK → `blank` template.
2. Add the repo files; Space runs the same `Dockerfile`.
3. Set `AGNES_API_KEY` and `AGNES_CORS_ORIGINS` under **Settings → Variables and secrets**.
4. Expose port `8765` in the Space's Dockerfile `EXPOSE` (already set).

## Option C — your own machine / VPS

```bash
docker compose up -d      # binds ./agnes_data for persistence
# or
./start.sh                # local Python, needs ffmpeg on PATH
```

## Environment variables

| Variable | Required | Notes |
| --- | --- | --- |
| `AGNES_API_KEY` | yes | Backend only. Never in `VITE_*` — Vite inlines those into public JS. |
| `AGNES_CORS_ORIGINS` | if frontend is on another origin | Comma-separated. `https://sebastisnzoth.github.io` for Pages. Empty = CORS off. |
| `AGNES_CORS_ENABLED` | no | `false` disables CORS even if origins are set. Default: auto (on when origins are set). |
| `PORT` | no | Default `8765`. |
| `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | no | Optional alternative text/image providers. |

Secrets are **not shared between platforms**. A GitHub Actions secret does not
reach Vercel or Render — configure each host separately.

## Wiring the Pages frontend to the backend

The Pages build embeds the public backend URL through
`VITE_AGNES_BACKEND_URL` (see `.github/workflows/pages.yml`). It is a build-time
*variable*, not a secret: it only tells the browser where to send `/api/*`.

Set it under **Settings → Secrets and variables → Actions → Variables** as
`AGNES_BACKEND_URL` = your backend URL, then re-run the Pages workflow.

`frontend/src/api/fetchPatch.ts` rewrites only same-origin `/api/...` calls, and
only accepts a value matching `https://host` — plain `http://` and anything with
a path are rejected and routing falls back to the page's own origin. If the
variable is unset, the frontend keeps calling its own origin and every API call
404s.

## Verifying a deployment

Run **Actions → Verify Deployment (E2E) → Run workflow**. It has two jobs:

1. **smoke** — Pages reachable and contains the Vue mount point; backend
   `/api/health` returns 200; `/api/config` does not leak a raw API key.
2. **real-generation** — calls the Agnes API directly with
   `secrets.AGNES_API_KEY`, polls to `completed`, downloads the file and probes
   it with `ffprobe`. The video is uploaded as a build artifact.

Without the `AGNES_API_KEY` secret, step 2 is reported as **SKIP**, not a
failure, so the smoke test is still useful on a fresh fork.

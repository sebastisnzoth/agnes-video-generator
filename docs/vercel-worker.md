# Vercel + persistent worker architecture

## Runtime split

- **Vercel**: frontend, FastAPI control plane, task validation and task status.
- **Supabase/PostgreSQL**: durable task state.
- **Supabase Storage**: uploaded inputs and completed videos.
- **Worker**: persistent process running Agnes API, TTS, FFmpeg and the existing pipelines.

The Vercel API never waits for a render to finish.

## Required production variables

Vercel:
- `AGNES_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_STORAGE_BUCKET` (default `agnes-videos`)

Worker:
- `AGNES_API_KEY`
- `AGNES_TASK_BACKEND=supabase`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_STORAGE_BUCKET`
- `AGNES_WORKER_POLL_SECONDS` (optional)

Never commit real keys.

## Supabase setup

Run `supabase_schema.sql` in the Supabase SQL editor. Create a Storage bucket named `agnes-videos`. Keep the bucket private; completed video URLs are generated as signed URLs.

## Run the worker

```bash
AGNES_TASK_BACKEND=supabase python -m worker
```

For Docker, use the existing image and run the worker command separately:

```bash
docker run --env-file .env agnes-video-generator python -m worker
```

## Local development

Without `VERCEL=1` and without `AGNES_TASK_BACKEND=supabase`, the queue uses SQLite under `AGNES_QUEUE_DB` and the original local task pipeline remains unchanged. This preserves the existing Docker/local workflow.

## Deployment gate

Do not deploy until:
1. Supabase schema is applied.
2. Vercel has the production environment variables.
3. A worker is running with the same Supabase project.
4. `pytest -q tests/test_remote_queue.py` passes.
5. An end-to-end task reaches `queued -> processing -> completed`.

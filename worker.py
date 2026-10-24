"""Persistent Agnes video worker. Run as a VM/container process, never as a Vercel request."""
from __future__ import annotations
import asyncio,logging,os
from core.remote_queue import claim_next_task,materialize_state,update_task,upload_file,signed_url
from core.config import get_api_key
from core.task_manager import TaskManager
from web import deps
logging.basicConfig(level=logging.INFO); log=logging.getLogger("agnes.worker")

def _find_output(v):
    if isinstance(v,dict):
        for x in v.values():
            r=_find_output(x)
            if r:return r
    if isinstance(v,list):
        for x in v:
            r=_find_output(x)
            if r:return r
    if isinstance(v,str) and os.path.isfile(v) and v.lower().endswith((".mp4",".webm",".mov")): return v
    return None

async def run_one(row):
    task_id=row["id"]; state,tmp=materialize_state(row["input"],task_id)
    tm=TaskManager(task_id,dir_name=row["dir_name"]); tm.create(state)
    key=get_api_key()
    if not key: raise RuntimeError("AGNES_API_KEY is not configured on worker")
    pipeline=deps.create_pipeline_for_type(state.task_type,key,task_id,row["dir_name"])
    done=asyncio.Event()
    async def sync_progress():
        while not done.is_set():
            try:
                current=tm.load()
                if current:
                    update_task(task_id,progress=float(getattr(current,"current_progress",0) or 0))
            except Exception:
                pass
            await asyncio.sleep(2)
    watcher=asyncio.create_task(sync_progress())
    try:
        await deps.run_pipeline_with_concurrency(pipeline,state,tm)
    finally:
        done.set()
        watcher.cancel()
        try: await watcher
        except asyncio.CancelledError: pass
    final=tm.load() or state; output=_find_output(final.model_dump(mode="python"))
    if not output: raise RuntimeError("Pipeline completed without a local video artifact")
    ref=upload_file(output,f"tasks/{task_id}/result/{os.path.basename(output)}")
    update_task(task_id,status="completed",progress=1,result_url=signed_url(ref),error="",completed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
    log.info("completed %s",task_id)

async def main():
    while True:
        row=claim_next_task()
        if not row:
            await asyncio.sleep(float(os.getenv("AGNES_WORKER_POLL_SECONDS","2"))); continue
        try: await run_one(row)
        except Exception as e:
            log.exception("task failed"); update_task(row["id"],status="failed",error=str(e)[:2000])

if __name__=="__main__": asyncio.run(main())

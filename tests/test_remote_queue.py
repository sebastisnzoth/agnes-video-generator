import os
from pathlib import Path

def test_sqlite_queue_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("AGNES_TASK_BACKEND", raising=False)
    monkeypatch.setenv("AGNES_QUEUE_DB", str(tmp_path / "queue.db"))
    from core import remote_queue
    remote_queue.create_task("t1","creative",{"task_id":"t1"},"dir1")
    row=remote_queue.get_task("t1")
    assert row["status"]=="queued"
    rows,total=remote_queue.list_tasks()
    assert total==1 and rows[0]["id"]=="t1"
    claimed=remote_queue.claim_next_task()
    assert claimed["id"]=="t1" and claimed["status"]=="processing"
    remote_queue.update_task("t1",status="completed",progress=1,result_url="file:///tmp/a.mp4")
    assert remote_queue.get_task("t1")["status"]=="completed"

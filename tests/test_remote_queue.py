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

def test_media_upload_streams_bytes_with_correct_mime(tmp_path, monkeypatch):
    from core import remote_queue
    file = tmp_path / "clip.mp4"
    file.write_bytes(b"sample-video-bytes")
    monkeypatch.setattr(remote_queue, "enabled", lambda: True)
    monkeypatch.setattr(remote_queue, "_storage", lambda: ("https://test.supabase.co", "test-role", "private"))
    observed = {}
    class FakeResponse:
        def raise_for_status(self):
            pass
    def fake_post(url, headers, data, timeout):
        observed.update(url=url, headers=headers, body=data.read(), timeout=timeout)
        return FakeResponse()
    monkeypatch.setattr(remote_queue.requests, "post", fake_post)
    ref = remote_queue.upload_file(str(file), "tasks/task123/result/clip.mp4")
    assert ref == "supabase://private/tasks/task123/result/clip.mp4"
    assert observed["headers"]["Content-Type"] == "video/mp4"
    assert observed["body"] == b"sample-video-bytes"
    assert observed["timeout"] == 120

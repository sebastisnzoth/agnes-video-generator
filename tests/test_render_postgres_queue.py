"""Render queue delegation checks with no external DB or network."""
from core import remote_queue

def test_postgres_queue_routes_to_dedicated_driver(monkeypatch):
    from core import postgres_queue
    monkeypatch.setenv("AGNES_TASK_BACKEND", "postgres")
    monkeypatch.setattr(postgres_queue, "create_task", lambda *args: {"id": args[0]})
    monkeypatch.setattr(postgres_queue, "get_task", lambda task_id: {"id":task_id})
    monkeypatch.setattr(postgres_queue, "claim_next_task", lambda: {"id":"job"})
    assert remote_queue.enabled()
    assert remote_queue.create_task("job","music_video",{},"dir") == {"id":"job"}
    assert remote_queue.get_task("job") == {"id":"job"}
    assert remote_queue.claim_next_task() == {"id":"job"}

def test_postgres_does_not_write_assets_without_private_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_TASK_BACKEND", "postgres")
    monkeypatch.delenv("AGNES_ASSET_BACKEND", raising=False)
    p=tmp_path/"song.mp3"
    p.write_bytes(b"audio")
    import pytest
    with pytest.raises(RuntimeError, match="Private asset storage"):
        remote_queue.upload_file(str(p),"tasks/job/inputs/song.mp3")

"""Worker output safety regressions; no remote provider calls."""
import asyncio
from types import SimpleNamespace
import pytest
import worker

def test_worker_fails_closed_without_remote_queue(monkeypatch):
    monkeypatch.setattr(worker, "enabled", lambda: False)
    with pytest.raises(RuntimeError, match="supabase"):
        asyncio.run(worker.main())

def test_worker_fails_closed_when_backend_is_local(monkeypatch):
    monkeypatch.setattr(worker, "enabled", lambda: True)
    monkeypatch.delenv("AGNES_TASK_BACKEND", raising=False)
    with pytest.raises(RuntimeError, match="supabase"):
        asyncio.run(worker.main())

def test_worker_rejects_scene_clip_as_final_result(monkeypatch, tmp_path):
    scene = tmp_path / "scene.mp4"
    scene.write_bytes(b"mock")
    state = SimpleNamespace(task_type="music_video", final_video_file="")
    class FakeManager:
        def __init__(self, *args, **kwargs): pass
        def create(self, state): pass
        def load(self): return state
    monkeypatch.setattr(worker, "materialize_state", lambda *_: (state, str(tmp_path)))
    monkeypatch.setattr(worker, "TaskManager", FakeManager)
    monkeypatch.setattr(worker, "get_api_key", lambda: "fake")
    monkeypatch.setattr(worker.deps, "create_pipeline_for_type", lambda *_: object())
    async def run(*args): pass
    monkeypatch.setattr(worker.deps, "run_pipeline_with_concurrency", run)
    monkeypatch.setattr(worker, "upload_file", lambda *_: pytest.fail("Never upload scene clip"))
    with pytest.raises(RuntimeError, match="final video artifact"):
        asyncio.run(worker.run_one({"id":"t1", "dir_name":"d1", "input":{}}))

"""Regression: music uploads must be durable before a serverless response returns."""
import os
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web.routes import task_creation_routes as routes
from web import helpers
from core import remote_queue

def test_music_video_uses_remote_queue_and_cleans_upload(monkeypatch, tmp_path):
    monkeypatch.setattr(routes, "get_api_key", lambda: "fake-key")
    monkeypatch.setattr(helpers, "get_upload_dir", lambda: str(tmp_path))
    monkeypatch.setattr(routes, "probe_duration", lambda path, default=0: 20)
    monkeypatch.setattr(remote_queue, "enabled", lambda: True)
    captured = []
    def enqueue(state, directory):
        assert os.path.exists(state.song_file)
        captured.append((state.task_id, directory))
    monkeypatch.setattr(remote_queue, "enqueue_state", enqueue)
    def never_run(*args, **kwargs):
        raise AssertionError("Local pipeline must not start in remote mode")
    monkeypatch.setattr(routes.deps, "create_pipeline_for_type", never_run)
    monkeypatch.setattr(routes.app_state, "launch_background_task", never_run)
    app = FastAPI()
    app.include_router(routes.router)
    result = TestClient(app).post("/api/tasks/music-video",
        files={"song": ("demo.mp3", b"ID3"+b"0"*128, "audio/mpeg")})
    assert result.status_code == 200, result.text
    assert len(captured) == 1
    assert captured[0][0] == result.json()["task_id"]
    assert list(tmp_path.iterdir()) == []

def test_music_video_queue_failure_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(routes, "get_api_key", lambda: "fake-key")
    monkeypatch.setattr(helpers, "get_upload_dir", lambda: str(tmp_path))
    monkeypatch.setattr(routes, "probe_duration", lambda path, default=0: 20)
    monkeypatch.setattr(remote_queue, "enabled", lambda: True)
    def broken(*args):
        raise ConnectionError("storage unavailable")
    monkeypatch.setattr(remote_queue, "enqueue_state", broken)
    app = FastAPI()
    app.include_router(routes.router)
    result = TestClient(app).post("/api/tasks/music-video",
        files={"song": ("demo.mp3", b"ID3"+b"0"*128, "audio/mpeg")})
    assert result.status_code == 503
    assert "storage unavailable" not in result.text
    assert list(tmp_path.iterdir()) == []

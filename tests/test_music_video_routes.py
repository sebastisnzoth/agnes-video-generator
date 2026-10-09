"""POST /api/tasks/music-video 路由测试（v7.1 音乐视频）。

覆盖错误码 400 / 413 / 422 与成功路径。写路径全部隔离到 tmp_path；
时长探测、后台任务、TaskManager、Pipeline 工厂均为替身，不触网、不调用 ffmpeg。

用法:
    .venv/bin/python -m pytest tests/test_music_video_routes.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.pipelines import music_video as mv  # noqa: E402
from models.task import MusicVideoTask, TaskType  # noqa: E402
from web import app_state, deps, helpers  # noqa: E402
from web.routes import task_creation_routes  # noqa: E402

URL = "/api/tasks/music-video"


@pytest.fixture(scope="module")
def client():
    app = FastAPI()
    app.include_router(task_creation_routes.router)
    return TestClient(app)


class _StubTaskManager:
    last = None

    def __init__(self, task_id, dir_name=None):
        self.task_id = task_id
        self.dir_name = dir_name
        self.state = None
        _StubTaskManager.last = self

    def create(self, state):
        self.state = state

    def update_state(self, **kwargs):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    """写路径隔离：API Key、上传目录、时长探测、Pipeline 工厂、后台任务、TaskManager 全部打桩。"""
    monkeypatch.setattr(task_creation_routes, "get_api_key", lambda: "test-api-key")
    uploads = str(tmp_path / "uploads")
    monkeypatch.setattr(helpers, "get_upload_dir", lambda: uploads)

    probe = {"seconds": 180.0}
    monkeypatch.setattr(task_creation_routes, "probe_duration", lambda path, default=0.0: probe["seconds"])

    factory_calls = []

    def fake_factory(task_type, api_key, task_id, dir_name):
        factory_calls.append(task_type)
        return object()

    monkeypatch.setattr(task_creation_routes.deps, "create_pipeline_for_type", fake_factory)
    launched = []

    def fake_launch(coro):
        launched.append(coro)
        coro.close()

    monkeypatch.setattr(task_creation_routes.app_state, "launch_background_task", fake_launch)
    monkeypatch.setattr(task_creation_routes, "TaskManager", _StubTaskManager)
    return {"uploads": uploads, "probe": probe, "launched": launched, "factory": factory_calls}


_MP3 = b"ID3" + b"\x00" * 128


def _song(name="my song.mp3", content=_MP3):
    return {"song": (name, content, "audio/mpeg")}


def _uploaded_files(uploads):
    return sorted(os.listdir(uploads)) if os.path.isdir(uploads) else []


# ═══════════════════════════════════════════════════
# 400 / 422：参数与格式校验
# ═══════════════════════════════════════════════════

class TestValidation:
    def test_missing_api_key_returns_400(self, client, env, monkeypatch):
        monkeypatch.setattr(task_creation_routes, "get_api_key", lambda: "")
        resp = client.post(URL, files=_song(), data={})
        assert resp.status_code == 400
        assert env["launched"] == []

    def test_unsupported_resolution_returns_422(self, client, env):
        resp = client.post(URL, files=_song(), data={"video_width": 1920, "video_height": 1080})
        assert resp.status_code == 422
        assert _uploaded_files(env["uploads"]) == []

    def test_missing_song_returns_422(self, client, env):
        resp = client.post(URL, data={})
        assert resp.status_code == 422
        assert env["launched"] == []

    def test_unsupported_extension_returns_422(self, client, env):
        resp = client.post(URL, files=_song("notes.txt", b"hello"), data={})
        assert resp.status_code == 422
        assert _uploaded_files(env["uploads"]) == []

    def test_empty_file_returns_422(self, client, env):
        resp = client.post(URL, files=_song("empty.mp3", b""), data={})
        assert resp.status_code == 422
        assert _uploaded_files(env["uploads"]) == []

    def test_duration_below_minimum_returns_422_and_removes_file(self, client, env):
        env["probe"]["seconds"] = 9.5
        resp = client.post(URL, files=_song(), data={})
        assert resp.status_code == 422
        assert _uploaded_files(env["uploads"]) == []

    def test_duration_above_maximum_returns_422(self, client, env):
        env["probe"]["seconds"] = 300.5
        resp = client.post(URL, files=_song(), data={})
        assert resp.status_code == 422
        assert _uploaded_files(env["uploads"]) == []

    def test_unreadable_song_returns_422(self, client, env):
        env["probe"]["seconds"] = 0.0
        resp = client.post(URL, files=_song(), data={})
        assert resp.status_code == 422
        assert _uploaded_files(env["uploads"]) == []


# ═══════════════════════════════════════════════════
# 413：分块读取超限
# ═══════════════════════════════════════════════════

class TestUploadLimit:
    def test_oversize_upload_returns_413_and_leaves_no_partial_file(self, client, env, monkeypatch):
        # 将上限压到 1 KB（真实值 50 MB 由常量定义；此处只验证超限分支与清理）
        monkeypatch.setattr(task_creation_routes, "MAX_SONG_BYTES", 1024)
        resp = client.post(URL, files=_song("big.mp3", b"\x01" * 4096), data={})
        assert resp.status_code == 413
        assert _uploaded_files(env["uploads"]) == []
        assert env["launched"] == []

    def test_real_limit_is_fifty_megabytes(self):
        assert mv.MAX_SONG_BYTES == 50 * 1024 * 1024

    def test_upload_at_limit_is_accepted(self, client, env, monkeypatch):
        monkeypatch.setattr(task_creation_routes, "MAX_SONG_BYTES", 1024)
        resp = client.post(URL, files=_song("edge.mp3", b"\x02" * 1024), data={})
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════
# 成功路径
# ═══════════════════════════════════════════════════

class TestCreate:
    def test_success_returns_task_id_and_queues_pipeline(self, client, env):
        resp = client.post(URL, files=_song(), data={"creative_name": "  demo  "})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert len(body["task_id"]) == 12
        assert body["dir_name"].endswith(body["task_id"])

        assert env["factory"] == [TaskType.MUSIC_VIDEO]
        assert len(env["launched"]) == 1
        state = _StubTaskManager.last.state
        assert isinstance(state, MusicVideoTask)
        assert state.creative_name == "demo"
        assert state.song_name == "my song.mp3"
        assert state.song_duration == 180.0
        assert state.clip_duration == 10
        assert state.style == mv.DEFAULT_MUSIC_VIDEO_STYLE
        assert state.lyrics_enabled is True
        assert state.subtitle_config.enabled is True
        assert (state.video_width, state.video_height) == (1280, 720)

    def test_upload_is_saved_under_uuid_name_with_allowed_extension(self, client, env):
        resp = client.post(URL, files=_song("../../evil name.WAV", b"RIFF" + b"\x00" * 64), data={})
        assert resp.status_code == 200
        state = _StubTaskManager.last.state
        saved = state.song_file
        assert os.path.dirname(saved) == env["uploads"]
        name = os.path.basename(saved)
        assert name.startswith("music_") and name.endswith(".wav")
        assert os.path.exists(saved)
        assert state.song_name == "evil name.WAV"

    def test_portrait_resolution_and_toggles_are_applied(self, client, env):
        resp = client.post(URL, files=_song(), data={
            "video_width": 768, "video_height": 1152,
            "style": "  noir jazz club  ", "lyrics_enabled": "false", "subtitle_enabled": "false",
        })
        assert resp.status_code == 200
        state = _StubTaskManager.last.state
        assert (state.video_width, state.video_height) == (768, 1152)
        assert state.style == "noir jazz club"
        assert state.lyrics_enabled is False
        assert state.subtitle_config.enabled is False

    def test_blank_style_falls_back_to_default(self, client, env):
        resp = client.post(URL, files=_song(), data={"style": "   "})
        assert resp.status_code == 200
        assert _StubTaskManager.last.state.style == mv.DEFAULT_MUSIC_VIDEO_STYLE

    def test_five_minute_song_is_accepted(self, client, env):
        env["probe"]["seconds"] = 300.0
        resp = client.post(URL, files=_song(), data={})
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════
# Pipeline 工厂分支
# ═══════════════════════════════════════════════════

class TestPipelineFactory:
    def test_factory_builds_music_video_pipeline(self, monkeypatch, tmp_path):
        monkeypatch.setattr(deps, "get_selected_models", lambda: {
            "text": "t", "image": "i", "video": "agnes-video-v2.0",
        })
        monkeypatch.setattr(app_state, "shutdown_event", None)
        monkeypatch.setattr("core.task_manager.get_working_dir", lambda: str(tmp_path))

        pipe = deps.create_pipeline_for_type(TaskType.MUSIC_VIDEO, "k", "t-music", "dir-music")

        assert isinstance(pipe, mv.MusicVideoPipeline)
        assert pipe.task_id == "t-music"

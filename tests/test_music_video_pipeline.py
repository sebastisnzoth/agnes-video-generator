"""MusicVideoPipeline 测试（v7.1 音乐视频）。

覆盖（对应 PRD §六 验收标准）：
- 分段：段数 = ceil(T/10)、末段为余数、5 分钟 = 30 段；
- 歌词：识别成功产出 lyrics.json / lyrics.srt / full_subtitle.srt；识别失败降级为无字幕、任务照常；
- 不调用 TTS；歌曲原样作为唯一音轨（音量 1.0，成片时长 ≈ 歌曲时长）；
- 断点续传：已完成的分段与成片不重复生成；
- 产物清单与依赖图的音乐视频分支见 tests/test_artifacts.py、tests/test_dependency_graph.py。

替身：视频 API、LLM、歌词识别、时长探测与转码均可替换；涉及真实合成的用例需要 ffmpeg，
无 ffmpeg 时自动跳过。

用法:
    .venv/bin/python -m pytest tests/test_music_video_pipeline.py -q
"""

import asyncio
import json
import math
import os
import re
import subprocess
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from core.audio.lyrics import LyricLine, LyricsUnavailableError  # noqa: E402
from core.compositor.ffmpeg_tool import has_audio_stream, probe_duration, resolve_binary  # noqa: E402
from core.pipelines import music_video as mv  # noqa: E402
from core.pipelines.music_video import MusicVideoPipeline  # noqa: E402
from models.task import (  # noqa: E402
    MusicVideoTask,
    SceneTask,
    StepStatus,
    SubtitleConfig,
)

needs_ffmpeg = pytest.mark.skipif(resolve_binary("ffmpeg") is None, reason="ffmpeg not available")


# ═══════════════════════════════════════════════════
# 替身与工厂
# ═══════════════════════════════════════════════════

class _StubTaskManager:
    def __init__(self, task_dir):
        self.task_dir = str(task_dir)
        self.calls = []

    def update_step(self, name, status):
        self.calls.append(("step", name, status))

    def update_state(self, **kwargs):
        self.calls.append(("state", kwargs))

    def create(self, state):
        return state


def _make_pipeline(tmp_path, state, *, screenwriter=None, video_api=None):
    """绕过 __init__ 的轻量构造（与 test_pipeline_subtitle_branches 同一约定），目录隔离到 tmp_path。"""
    pipe = object.__new__(MusicVideoPipeline)
    pipe.task_manager = _StubTaskManager(tmp_path)
    pipe.task_id = "mv-test"
    pipe.dir_name = "mv-test"
    pipe.api_key = "test-key"
    pipe.progress_callback = None
    pipe.shutdown_event = None
    pipe._stop_event = asyncio.Event()
    pipe.screenwriter = screenwriter if screenwriter is not None else mock.MagicMock()
    pipe.video_api = video_api if video_api is not None else mock.MagicMock()
    pipe._state = state
    pipe._emit = mock.AsyncMock()
    return pipe


def _state(tmp_path, *, lyrics=True, subtitle=True, style="", width=320, height=240):
    src = tmp_path / "uploads" / "music_src.mp3"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"source-bytes")
    return MusicVideoTask(
        task_id="mv-test",
        creative_name="mv",
        song_name="song.mp3",
        song_file=str(src),
        style=style,
        lyrics_enabled=lyrics,
        video_width=width,
        video_height=height,
        subtitle_config=SubtitleConfig(enabled=subtitle, style=mv.MUSIC_SUBTITLE_STYLE),
    )


@pytest.fixture
def song_env(monkeypatch):
    """替换转码与时长探测（不依赖 ffmpeg）：转码只写占位字节，时长由 env['duration'] 决定。"""
    env = {"duration": 25.0}

    def fake_transcode(src, dst):
        with open(dst, "wb") as f:
            f.write(b"ID3-placeholder")
        return dst

    monkeypatch.setattr(mv, "transcode_song_to_mp3", fake_transcode)
    monkeypatch.setattr(mv, "probe_duration", lambda path, default=0.0: env["duration"])
    return env


def _stub_lyrics(monkeypatch, lines=None, error=None):
    calls = []

    def fake(path):
        calls.append(path)
        if error is not None:
            raise error
        return list(lines or [])

    monkeypatch.setattr(mv, "transcribe_lyrics", fake)
    return calls


def _writer(prompts=None, error=None):
    sw = mock.MagicMock()
    if error is not None:
        sw.generate_music_video_prompts.side_effect = error
    else:
        sw.generate_music_video_prompts.return_value = list(prompts or [])
    return sw


def _mean_volume(path):
    """用 ffmpeg volumedetect 读取平均响度（dB）。"""
    proc = subprocess.run(
        [resolve_binary("ffmpeg"), "-hide_banner", "-i", path, "-vn",
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    match = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    assert match, proc.stderr[-400:]
    return float(match.group(1))


def _make_clip(path, seconds, width=160, height=120):
    """真实的 H.264 片段（testsrc 画面、无音轨），用于端到端合成。"""
    subprocess.run(
        [resolve_binary("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=25",
         "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=120,
    )
    return str(path)


def _make_tone(path, seconds):
    """真实的 WAV 正弦测试音（用于歌曲转码与响度比较）。"""
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    subprocess.run(
        [resolve_binary("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "pcm_s16le", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=120,
    )
    return str(path)


class _FakeClipOutput:
    """替身视频输出：save() 写出一段真实 mp4，时长 = 请求时长 + 0.04 秒（模拟 Agnes 帧数取整）。"""

    def __init__(self, seconds):
        self.seconds = seconds

    async def save(self, path):
        _make_clip(path, self.seconds)


# ═══════════════════════════════════════════════════
# 构造与基础钩子
# ═══════════════════════════════════════════════════

class TestConstruction:
    def test_constructor_wires_video_and_text_clients(self, tmp_path):
        pipe = MusicVideoPipeline(api_key="k", task_id="t1", dir_name="d1")
        assert pipe.task_id == "t1"
        assert pipe.video_api is not None
        assert pipe.screenwriter is not None

    def test_no_manual_pause_points(self, tmp_path):
        pipe = _make_pipeline(tmp_path, _state(tmp_path))
        assert pipe._get_pausable_steps() == set()

    def test_template_prompt_has_no_lyrics_and_no_text(self):
        prompt = MusicVideoPipeline._template_prompt("neon city", 1, 4)
        assert prompt == "neon city, segment 2 of 4, no on-screen text"

    def test_lyric_window_picks_overlapping_lines_only(self):
        lines = [LyricLine(1.0, 4.0, "甲"), LyricLine(9.5, 12.0, "乙"), LyricLine(30.0, 31.0, "丙")]
        assert MusicVideoPipeline._lyric_window(lines, 0.0, 10.0) == "甲 / 乙"
        assert MusicVideoPipeline._lyric_window(lines, 10.0, 20.0) == "乙"
        assert MusicVideoPipeline._lyric_window(lines, 20.0, 25.0) == mv.INSTRUMENTAL_TAG


# ═══════════════════════════════════════════════════
# Phase 1：分段与歌词
# ═══════════════════════════════════════════════════

class TestBuildScenes:
    def test_lyrics_success_builds_spans_prompts_and_files(self, tmp_path, song_env, monkeypatch):
        song_env["duration"] = 25.0
        lines = [
            LyricLine(1.0, 4.0, "第一行"),
            LyricLine(11.0, 14.0, "第二行"),
            LyricLine(21.0, 24.0, "第三行"),
        ]
        calls = _stub_lyrics(monkeypatch, lines=lines)
        sw = _writer(prompts=["p0", "p1", "p2"])
        pipe = _make_pipeline(tmp_path, _state(tmp_path), screenwriter=sw)

        asyncio.run(pipe._build_scenes())

        st = pipe.state
        assert len(calls) == 1
        assert st.song_duration == 25.0
        assert st.scene_spans == [[0.0, 10.0], [10.0, 20.0], [20.0, 25.0]]
        assert [s.duration for s in st.scenes] == [10, 10, 5]
        assert [s.scene_prompt for s in st.scenes] == ["p0", "p1", "p2"]
        assert [s.narration_text for s in st.scenes] == ["第一行", "第二行", "第三行"]
        assert len(st.lyric_lines) == 3
        assert (tmp_path / "lyrics.json").exists()
        assert (tmp_path / "lyrics.srt").exists()
        style, windows = sw.generate_music_video_prompts.call_args[0]
        assert style == mv.DEFAULT_MUSIC_VIDEO_STYLE
        assert windows == ["第一行", "第二行", "第三行"]
        saved = json.loads((tmp_path / "prompts.json").read_text(encoding="utf-8"))
        assert saved["prompt_source"] == "llm"
        assert saved["scene_spans"] == st.scene_spans

    def test_scene_count_is_ceil_of_duration_for_five_minutes(self, tmp_path, song_env, monkeypatch):
        song_env["duration"] = 300.0
        _stub_lyrics(monkeypatch, lines=[])
        sw = _writer(prompts=[f"p{i}" for i in range(30)])
        pipe = _make_pipeline(tmp_path, _state(tmp_path, lyrics=False), screenwriter=sw)

        asyncio.run(pipe._build_scenes())

        assert len(pipe.state.scenes) == 30
        assert all(s.duration == 10 for s in pipe.state.scenes)
        assert pipe.state.scene_spans[-1] == [290.0, 300.0]

    def test_remainder_span_is_exact_and_requests_at_least_three_seconds(self, tmp_path, song_env, monkeypatch):
        song_env["duration"] = 20.5
        _stub_lyrics(monkeypatch, lines=[])
        sw = _writer(prompts=["a", "b", "c"])
        pipe = _make_pipeline(tmp_path, _state(tmp_path, lyrics=False), screenwriter=sw)

        asyncio.run(pipe._build_scenes())

        assert pipe.state.scene_spans[-1] == [20.0, 20.5]
        assert [s.duration for s in pipe.state.scenes] == [10, 10, 3]

    def test_lyrics_failure_degrades_without_subtitles(self, tmp_path, song_env, monkeypatch):
        _stub_lyrics(monkeypatch, error=LyricsUnavailableError("model missing"))
        pipe = _make_pipeline(tmp_path, _state(tmp_path), screenwriter=_writer(prompts=["a", "b", "c"]))

        asyncio.run(pipe._build_scenes())
        asyncio.run(pipe._generate_subtitles(None))

        st = pipe.state
        assert st.lyric_lines == []
        assert len(st.scenes) == 3
        assert all(s.narration_text == "" for s in st.scenes)
        assert not (tmp_path / "lyrics.json").exists()
        assert not (tmp_path / "lyrics.srt").exists()
        assert not (tmp_path / "full_subtitle.srt").exists()
        assert st.combined_subtitle == ""

    def test_lyrics_disabled_skips_recognition(self, tmp_path, song_env, monkeypatch):
        calls = _stub_lyrics(monkeypatch, lines=[LyricLine(0.0, 1.0, "x")])
        pipe = _make_pipeline(tmp_path, _state(tmp_path, lyrics=False), screenwriter=_writer(prompts=["a", "b", "c"]))

        asyncio.run(pipe._build_scenes())

        assert calls == []
        assert not (tmp_path / "lyrics.json").exists()

    def test_instrumental_song_writes_lyrics_json_without_srt(self, tmp_path, song_env, monkeypatch):
        _stub_lyrics(monkeypatch, lines=[])
        pipe = _make_pipeline(tmp_path, _state(tmp_path), screenwriter=_writer(prompts=["a", "b", "c"]))

        asyncio.run(pipe._build_scenes())
        asyncio.run(pipe._generate_subtitles(None))

        assert (tmp_path / "lyrics.json").exists()
        assert not (tmp_path / "lyrics.srt").exists()
        assert not (tmp_path / "full_subtitle.srt").exists()

    def test_llm_failure_uses_template_prompts(self, tmp_path, song_env, monkeypatch):
        song_env["duration"] = 25.0
        _stub_lyrics(monkeypatch, lines=[LyricLine(1.0, 4.0, "secret lyric")])
        pipe = _make_pipeline(tmp_path, _state(tmp_path), screenwriter=_writer(error=ValueError("bad json")))

        asyncio.run(pipe._build_scenes())

        prompts = [s.scene_prompt for s in pipe.state.scenes]
        assert prompts[0] == f"{mv.DEFAULT_MUSIC_VIDEO_STYLE}, segment 1 of 3, no on-screen text"
        assert all("secret lyric" not in p for p in prompts)
        saved = json.loads((tmp_path / "prompts.json").read_text(encoding="utf-8"))
        assert saved["prompt_source"] == "template"

    def test_partial_llm_output_is_filled_by_template(self, tmp_path, song_env, monkeypatch):
        _stub_lyrics(monkeypatch, lines=[])
        pipe = _make_pipeline(tmp_path, _state(tmp_path), screenwriter=_writer(prompts=["only-first"]))

        asyncio.run(pipe._build_scenes())

        prompts = [s.scene_prompt for s in pipe.state.scenes]
        assert prompts[0] == "only-first"
        assert prompts[1].startswith(mv.DEFAULT_MUSIC_VIDEO_STYLE)
        saved = json.loads((tmp_path / "prompts.json").read_text(encoding="utf-8"))
        assert saved["prompt_source"] == "partial"

    def test_resume_reuses_existing_scenes(self, tmp_path, song_env, monkeypatch):
        calls = _stub_lyrics(monkeypatch, lines=[])
        sw = _writer(prompts=["x"])
        state = _state(tmp_path)
        state.scenes = [SceneTask(index=0, scene_prompt="kept", duration=10)]
        state.scene_spans = [[0.0, 10.0]]
        pipe = _make_pipeline(tmp_path, state, screenwriter=sw)

        asyncio.run(pipe._build_scenes())

        assert calls == []
        sw.generate_music_video_prompts.assert_not_called()
        assert pipe.state.scenes[0].scene_prompt == "kept"

    def test_missing_song_raises(self, tmp_path, song_env, monkeypatch):
        _stub_lyrics(monkeypatch, lines=[])
        state = _state(tmp_path)
        state.song_file = str(tmp_path / "does-not-exist.mp3")
        pipe = _make_pipeline(tmp_path, state, screenwriter=_writer(prompts=["a"]))
        with pytest.raises(RuntimeError):
            asyncio.run(pipe._build_scenes())

    def test_unreadable_duration_raises(self, tmp_path, song_env, monkeypatch):
        song_env["duration"] = 0.0
        _stub_lyrics(monkeypatch, lines=[])
        pipe = _make_pipeline(tmp_path, _state(tmp_path), screenwriter=_writer(prompts=["a"]))
        with pytest.raises(RuntimeError):
            asyncio.run(pipe._build_scenes())


# ═══════════════════════════════════════════════════
# Phase 2–5：视频续传、音频（无 TTS）、字幕
# ═══════════════════════════════════════════════════

class TestVideoResume:
    def test_finished_scene_videos_are_not_resubmitted(self, tmp_path):
        state = _state(tmp_path)
        state.scenes = [
            SceneTask(index=0, scene_prompt="a", duration=10),
            SceneTask(index=1, scene_prompt="b", duration=10),
            SceneTask(index=2, scene_prompt="c", duration=5),
        ]
        state.scene_spans = [[0.0, 10.0], [10.0, 20.0], [20.0, 25.0]]
        done = tmp_path / "scene_0"
        done.mkdir()
        (done / "video.mp4").write_bytes(b"finished-clip")

        submitted = []

        async def fake_submit(**kwargs):
            submitted.append(kwargs)
            return f"vid-{len(submitted)}"

        video_api = mock.MagicMock()
        video_api.submit_video = mock.AsyncMock(side_effect=fake_submit)
        video_api.wait_for_video = mock.AsyncMock(side_effect=lambda vid: _FakeClipOutput(4.0))
        pipe = _make_pipeline(tmp_path, state, video_api=video_api)

        asyncio.run(pipe._generate_videos())

        assert [k["duration"] for k in submitted] == [10, 5]
        assert all(k["width"] == 320 and k["height"] == 240 for k in submitted)
        assert state.scenes[0].video_file == str(done / "video.mp4")
        assert os.path.exists(tmp_path / "scene_1" / "video.mp4")
        assert os.path.exists(tmp_path / "scene_2" / "video.mp4")


class TestSingerAndStoryboard:
    """v7.2 歌手/演员参考图（none/photo/ai）与分镜故事板 storyboard.json。"""

    @staticmethod
    def _png_photo(tmp_path):
        from PIL import Image

        src = tmp_path / "uploads" / "face.png"
        src.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), (200, 30, 30)).save(src, "PNG")
        return str(src)

    @staticmethod
    def _img_api(*, fail=False, prompt_recorder=None):
        """图片 API 替身：generate_single_image(prompt=, size=) → save(dst) 写字节。"""
        api = mock.MagicMock()

        async def _gen(**kwargs):
            if prompt_recorder is not None:
                prompt_recorder.append(kwargs)
            if fail:
                raise RuntimeError("image api down")

            class _Out:
                async def save(self, dst):
                    with open(dst, "wb") as f:
                        f.write(b"\x89PNG\r\n\x1a\nfake-bytes")

            return _Out()

        api.generate_single_image = mock.AsyncMock(side_effect=_gen)
        return api

    # ── Phase 2：_build_reference_images ──────────────────────────

    def test_none_mode_is_noop_without_image_api(self, tmp_path):
        state = _state(tmp_path)
        pipe = _make_pipeline(tmp_path, state)
        # none 模式不得触碰 image_generator（_make_pipeline 未注入该属性）
        asyncio.run(pipe._build_reference_images())

        assert state.singer_image == ""
        assert not (tmp_path / "singer.png").exists()

    def test_photo_mode_stages_png_into_working_dir(self, tmp_path):
        state = _state(tmp_path)
        state.singer_mode = "photo"
        state.singer_photo = self._png_photo(tmp_path)
        pipe = _make_pipeline(tmp_path, state)

        asyncio.run(pipe._build_reference_images())

        dst = tmp_path / "singer.png"
        assert dst.exists() and dst.read_bytes().startswith(b"\x89PNG")
        assert state.singer_image == str(dst)
        assert ("state", {"singer_image": str(dst)}) in pipe.task_manager.calls

    def test_photo_mode_missing_source_fails_task(self, tmp_path):
        state = _state(tmp_path)
        state.singer_mode = "photo"
        state.singer_photo = str(tmp_path / "uploads" / "gone.png")
        pipe = _make_pipeline(tmp_path, state)

        with pytest.raises(RuntimeError, match="找不到|missing"):
            asyncio.run(pipe._build_reference_images())

    def test_photo_mode_invalid_bytes_fails_task(self, tmp_path):
        state = _state(tmp_path)
        state.singer_mode = "photo"
        src = tmp_path / "uploads" / "bad.png"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"not-an-image")
        state.singer_photo = str(src)
        pipe = _make_pipeline(tmp_path, state)

        with pytest.raises(RuntimeError):
            asyncio.run(pipe._build_reference_images())
        assert not (tmp_path / "singer.png").exists()

    def test_ai_mode_generates_image_via_api(self, tmp_path):
        state = _state(tmp_path)
        state.singer_mode = "ai"
        state.singer_prompt = "a rock star with a red guitar"
        calls = []
        pipe = _make_pipeline(tmp_path, state)
        pipe.image_generator = self._img_api(prompt_recorder=calls)

        asyncio.run(pipe._build_reference_images())

        assert calls == [{
            "prompt": "a rock star with a red guitar",
            "size": f"{state.video_width}x{state.video_height}",
        }]
        dst = tmp_path / "singer.png"
        assert dst.exists() and dst.stat().st_size > 0
        assert state.singer_image == str(dst)

    def test_ai_mode_api_failure_fails_task(self, tmp_path):
        state = _state(tmp_path)
        state.singer_mode = "ai"
        state.singer_prompt = "x"
        pipe = _make_pipeline(tmp_path, state)
        pipe.image_generator = self._img_api(fail=True)

        with pytest.raises(RuntimeError, match="失败|failed"):
            asyncio.run(pipe._build_reference_images())
        assert not (tmp_path / "singer.png").exists()

    def test_existing_singer_image_skips_regeneration(self, tmp_path):
        state = _state(tmp_path)
        state.singer_mode = "ai"
        state.singer_prompt = "x"
        (tmp_path / "singer.png").write_bytes(b"\x89PNG\r\n\x1a\npreexisting")
        pipe = _make_pipeline(tmp_path, state)
        pipe.image_generator = mock.MagicMock()
        pipe.image_generator.generate_single_image = mock.AsyncMock(
            side_effect=AssertionError("must not be called on resume"),
        )

        asyncio.run(pipe._build_reference_images())

        assert state.singer_image == str(tmp_path / "singer.png")
        pipe.image_generator.generate_single_image.assert_not_called()

    # ── Phase 3：_get_scene_ref_images → 每段提交携带参考图 ────────

    def test_get_scene_ref_images_states(self, tmp_path):
        state = _state(tmp_path)
        pipe = _make_pipeline(tmp_path, state)
        assert pipe._get_scene_ref_images(None, 0) == []

        existing = tmp_path / "singer.png"
        existing.write_bytes(b"\x89PNG")
        state.singer_image = str(existing)
        assert pipe._get_scene_ref_images(None, 0) == [str(existing)]

        state.singer_image = str(tmp_path / "gone.png")
        assert pipe._get_scene_ref_images(None, 0) == []

    def test_generate_videos_passes_reference_image_paths(self, tmp_path):
        state = _state(tmp_path)
        state.scenes = [SceneTask(index=0, scene_prompt="a", duration=10)]
        state.scene_spans = [[0.0, 10.0]]
        singer = tmp_path / "singer.png"
        singer.write_bytes(b"\x89PNG")
        state.singer_image = str(singer)

        submitted = []

        async def fake_submit(**kwargs):
            submitted.append(kwargs)
            return "vid-1"

        video_api = mock.MagicMock()
        video_api.submit_video = mock.AsyncMock(side_effect=fake_submit)
        video_api.wait_for_video = mock.AsyncMock(
            side_effect=lambda vid: _FakeClipOutput(4.0),
        )
        pipe = _make_pipeline(tmp_path, state, video_api=video_api)

        asyncio.run(pipe._generate_videos())

        assert len(submitted) == 1
        assert submitted[0]["reference_image_paths"] == [str(singer)]

    # ── Phase 1：storyboard.json ──────────────────────────────────

    def test_build_scenes_writes_storyboard(self, tmp_path, song_env, monkeypatch):
        song_env["duration"] = 25.0
        lines = [LyricLine(1.0, 4.0, "第一行"), LyricLine(11.0, 14.0, "第二行")]
        _stub_lyrics(monkeypatch, lines=lines)
        sw = _writer(prompts=["p0", "p1", "p2"])
        pipe = _make_pipeline(tmp_path, _state(tmp_path), screenwriter=sw)

        asyncio.run(pipe._build_scenes())

        sb = json.loads((tmp_path / "storyboard.json").read_text(encoding="utf-8"))
        assert sb["format_version"] == "1.0"
        assert sb["song"] == {"name": "song.mp3", "duration": 25.0}
        assert sb["prompt_source"] == "llm"
        assert sb["singer"] == {"mode": "none", "description": "", "image": None}
        assert len(sb["segments"]) == 3
        assert sb["segments"][0] == {
            "index": 0, "start": 0.0, "end": 10.0,
            "lyrics": "第一行", "visual": "p0",
        }
        # 无歌词段 → instrumental 占位
        assert sb["segments"][2]["lyrics"] == mv.INSTRUMENTAL_TAG
        assert [s["visual"] for s in sb["segments"]] == ["p0", "p1", "p2"]

    def test_build_scenes_storyboard_records_singer(self, tmp_path, song_env, monkeypatch):
        song_env["duration"] = 25.0
        _stub_lyrics(monkeypatch, lines=[])
        state = _state(tmp_path)
        state.singer_mode = "ai"
        state.singer_prompt = "a silver-haired singer"
        pipe = _make_pipeline(tmp_path, state, screenwriter=_writer(prompts=["a", "b", "c"]))

        asyncio.run(pipe._build_scenes())

        sb = json.loads((tmp_path / "storyboard.json").read_text(encoding="utf-8"))
        assert sb["singer"] == {
            "mode": "ai",
            "description": "a silver-haired singer",
            "image": "singer.png",
        }

    def test_resume_backfills_missing_storyboard_from_state(self, tmp_path, song_env, monkeypatch):
        state = _state(tmp_path)
        state.scenes = [SceneTask(index=0, scene_prompt="pa", duration=10)]
        state.scene_spans = [[0.0, 10.0]]
        state.lyric_lines = [{"start": 1.0, "end": 2.0, "text": "hi"}]
        (tmp_path / "prompts.json").write_text(
            json.dumps({"prompt_source": "template"}), encoding="utf-8",
        )
        pipe = _make_pipeline(tmp_path, state, screenwriter=_writer())

        asyncio.run(pipe._build_scenes())  # scenes 存在 → SKIP 分支 + 补写

        sb = json.loads((tmp_path / "storyboard.json").read_text(encoding="utf-8"))
        assert sb["prompt_source"] == "template"
        assert sb["segments"] == [{
            "index": 0, "start": 0.0, "end": 10.0, "lyrics": "hi", "visual": "pa",
        }]
        pipe.screenwriter.generate_music_video_prompts.assert_not_called()

    # ── performer 注入（LLM 感知歌手）──────────────────────────────

    def test_performer_kwarg_none_mode_is_empty(self, tmp_path, song_env, monkeypatch):
        _stub_lyrics(monkeypatch, lines=[])
        sw = _writer(prompts=["a", "b", "c"])
        pipe = _make_pipeline(tmp_path, _state(tmp_path), screenwriter=sw)

        asyncio.run(pipe._build_scenes())

        style, windows = sw.generate_music_video_prompts.call_args[0]
        assert style == mv.DEFAULT_MUSIC_VIDEO_STYLE
        assert sw.generate_music_video_prompts.call_args.kwargs.get("performer") == ""

    def test_performer_kwarg_uses_singer_prompt(self, tmp_path, song_env, monkeypatch):
        _stub_lyrics(monkeypatch, lines=[])
        state = _state(tmp_path)
        state.singer_mode = "ai"
        state.singer_prompt = "a cyberpunk diva"
        sw = _writer(prompts=["a", "b", "c"])
        pipe = _make_pipeline(tmp_path, state, screenwriter=sw)

        asyncio.run(pipe._build_scenes())

        assert sw.generate_music_video_prompts.call_args.kwargs["performer"] == "a cyberpunk diva"

    def test_resolve_performer_text_modes(self, tmp_path):
        state = _state(tmp_path)
        pipe = _make_pipeline(tmp_path, state)
        assert pipe._resolve_performer_text() == ""

        state.singer_mode = "photo"
        assert pipe._resolve_performer_text() == mv._PERFORMER_FROM_REFERENCE
        state.singer_prompt = "  custom desc  "
        assert pipe._resolve_performer_text() == "custom desc"

        state.singer_mode = "ai"
        state.singer_prompt = ""
        assert pipe._resolve_performer_text() == mv._PERFORMER_GENERIC

    def test_template_prompt_with_and_without_performer(self):
        old = MusicVideoPipeline._template_prompt("neon city", 1, 4)
        assert old == "neon city, segment 2 of 4, no on-screen text"
        new = MusicVideoPipeline._template_prompt("neon city", 1, 4, "a singer")
        assert new == "neon city, a singer, segment 2 of 4, no on-screen text"

    def test_template_fallback_embeds_performer(self, tmp_path, song_env, monkeypatch):
        _stub_lyrics(monkeypatch, lines=[])
        state = _state(tmp_path)
        state.singer_mode = "ai"
        state.singer_prompt = "a jazz singer"
        sw = _writer(error=ValueError("no llm"))
        pipe = _make_pipeline(tmp_path, state, screenwriter=sw)

        asyncio.run(pipe._build_scenes())

        assert all("a jazz singer" in s.scene_prompt for s in state.scenes)
        sb = json.loads((tmp_path / "storyboard.json").read_text(encoding="utf-8"))
        assert sb["prompt_source"] == "template"


class TestAudioAndSubtitles:
    def test_audio_step_transcodes_song_without_tts(self, tmp_path, song_env, monkeypatch):
        pipe = _make_pipeline(tmp_path, _state(tmp_path))
        pipe._generate_audio_with_fallback = mock.AsyncMock(side_effect=AssertionError("TTS must not run"))

        result = asyncio.run(pipe._generate_audio())

        assert result is None
        pipe._generate_audio_with_fallback.assert_not_called()
        song = tmp_path / mv.SONG_FILENAME
        assert song.exists()
        assert pipe.state.combined_audio == str(song)

    def test_subtitles_copy_lyrics_srt(self, tmp_path, song_env, monkeypatch):
        _stub_lyrics(monkeypatch, lines=[LyricLine(1.0, 3.0, "第一句"), LyricLine(5.0, 7.0, "第二句")])
        pipe = _make_pipeline(tmp_path, _state(tmp_path), screenwriter=_writer(prompts=["a", "b", "c"]))
        asyncio.run(pipe._build_scenes())

        asyncio.run(pipe._generate_subtitles(None))

        full = tmp_path / mv.SUBTITLE_SRT_FILENAME
        assert full.read_bytes() == (tmp_path / mv.LYRICS_SRT_FILENAME).read_bytes()
        assert pipe.state.combined_subtitle == str(full)
        assert pipe.state.subtitle_styles_path == ""

    def test_subtitles_disabled_writes_nothing(self, tmp_path, song_env, monkeypatch):
        _stub_lyrics(monkeypatch, lines=[LyricLine(1.0, 3.0, "句")])
        pipe = _make_pipeline(tmp_path, _state(tmp_path, subtitle=False), screenwriter=_writer(prompts=["a", "b", "c"]))
        asyncio.run(pipe._build_scenes())

        asyncio.run(pipe._generate_subtitles(None))

        assert not (tmp_path / mv.SUBTITLE_SRT_FILENAME).exists()
        assert pipe.state.combined_subtitle == ""


# ═══════════════════════════════════════════════════
# Phase 6：合成（需要 ffmpeg）
# ═══════════════════════════════════════════════════

class TestComposite:
    def test_resume_returns_existing_final_video(self, tmp_path):
        (tmp_path / mv.FINAL_VIDEO_FILENAME).write_bytes(b"final")
        pipe = _make_pipeline(tmp_path, _state(tmp_path))
        assert asyncio.run(pipe._composite_final()) == str(tmp_path / mv.FINAL_VIDEO_FILENAME)

    def test_missing_clip_raises(self, tmp_path):
        state = _state(tmp_path)
        state.scenes = [SceneTask(index=0, scene_prompt="a", duration=10, video_file="")]
        state.scene_spans = [[0.0, 10.0]]
        pipe = _make_pipeline(tmp_path, state)
        with pytest.raises(RuntimeError):
            asyncio.run(pipe._composite_final())

    @needs_ffmpeg
    def test_song_is_the_only_audio_and_length_matches(self, tmp_path):
        """成片音轨即歌曲本身：时长 ≈ 歌曲（±0.2 s），平均响度与歌曲一致（不放大 1.5 倍）。"""
        song = _make_tone(tmp_path / "song.wav", 6.0)
        clips = [_make_clip(tmp_path / f"c{i}.mp4", 1.0) for i in range(3)]
        spans = [[0.0, 2.5], [2.5, 4.0], [4.0, 6.0]]
        state = _state(tmp_path)
        state.combined_audio = song
        state.scene_spans = spans
        state.scenes = [
            SceneTask(index=i, scene_prompt="p", duration=5, video_file=clips[i]) for i in range(3)
        ]
        pipe = _make_pipeline(tmp_path, state)

        out = asyncio.run(pipe._composite_final())

        assert out == str(tmp_path / mv.FINAL_VIDEO_FILENAME)
        assert probe_duration(out) == pytest.approx(6.0, abs=0.2)
        assert has_audio_stream(out) is True
        assert _mean_volume(out) == pytest.approx(_mean_volume(song), abs=1.0)
        assert not (tmp_path / mv.TIMELINE_FILENAME).exists()


# ═══════════════════════════════════════════════════
# 端到端 run()（需要 ffmpeg；视频 API / LLM / 歌词识别为替身）
# ═══════════════════════════════════════════════════

@needs_ffmpeg
class TestEndToEnd:
    def test_full_run_produces_song_length_video_without_tts(self, tmp_path, monkeypatch):
        song = _make_tone(tmp_path / "uploads" / "song.wav", 23.0)
        lines = [LyricLine(1.0, 5.0, "第一句"), LyricLine(12.0, 16.0, "第二句")]
        monkeypatch.setattr(mv, "transcribe_lyrics", lambda path: list(lines))

        submitted = []

        async def fake_submit(**kwargs):
            submitted.append(kwargs)
            return f"vid-{len(submitted) - 1}"

        video_api = mock.MagicMock()
        video_api.submit_video = mock.AsyncMock(side_effect=fake_submit)
        # Agnes 的片段时长会略超请求时长（帧数取整），以此模拟真实情况
        video_api.wait_for_video = mock.AsyncMock(
            side_effect=lambda vid: _FakeClipOutput(submitted[int(vid.split("-")[1])]["duration"] + 0.04)
        )
        sw = _writer(prompts=["scene a", "scene b", "scene c"])
        state = _state(tmp_path, lyrics=True, subtitle=False)
        state.song_file = song
        pipe = _make_pipeline(tmp_path, state, screenwriter=sw, video_api=video_api)
        pipe._generate_audio_with_fallback = mock.AsyncMock(side_effect=AssertionError("TTS must not run"))

        async def _no_watermark(path):
            return path

        pipe._apply_watermark = _no_watermark

        final = asyncio.run(pipe.run(state))

        assert state.status == StepStatus.COMPLETED
        assert state.final_video_file == final == str(tmp_path / mv.FINAL_VIDEO_FILENAME)
        # 请求时长 = max(3, ceil(跨度))；跨度来自 mp3 的探测时长（含编码器填充，约 23.03 秒）
        expected = [max(3, math.ceil(round(e - s, 6))) for s, e in state.scene_spans]
        assert len(submitted) == 3
        assert [k["duration"] for k in submitted] == expected
        assert expected[:2] == [10, 10]
        assert probe_duration(final) == pytest.approx(23.0, abs=0.2)
        assert has_audio_stream(final) is True
        assert _mean_volume(final) == pytest.approx(_mean_volume(song), abs=1.0)
        assert (tmp_path / mv.LYRICS_JSON_FILENAME).exists()
        assert not (tmp_path / mv.TIMELINE_FILENAME).exists()

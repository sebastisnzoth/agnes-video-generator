"""
单元测试：core.compositor.ffmpeg_tool — ffmpeg/ffprobe 可执行文件解析。

覆盖 resolve_binary / resolve_ffmpeg / resolve_ffprobe / _resolve /
_resolve_builtin_ffmpeg / _sibling 的全部分支：
- 环境变量显式指定优先；
- 系统 PATH 兜底；
- imageio-ffmpeg 内置二进制；
- 全部不可用时返回 None；
- 未知二进制名抛 ValueError；
- 进程级缓存。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import core.compositor.ffmpeg_tool as ft


@pytest.fixture(autouse=True)
def _clear_cache():
    ft._cache.clear()
    yield
    ft._cache.clear()


def test_unknown_binary_raises():
    with pytest.raises(ValueError):
        ft.resolve_binary("unknown-prog")


def test_explicit_env_override(monkeypatch, tmp_path):
    """环境变量显式指定且存在 → 优先返回。"""
    fake = tmp_path / "ffmpeg-custom"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("FFMPEG_BINARY", str(fake))
    monkeypatch.setenv("FFPROBE_BINARY", "")
    # 确保 PATH 与内置不可干扰
    monkeypatch.setattr(ft.shutil, "which", lambda name: None)
    monkeypatch.setattr(ft, "_resolve_builtin_ffmpeg", lambda: None)

    assert ft.resolve_ffmpeg() == str(fake)


def test_system_path_fallback(monkeypatch):
    """env 未指定 → 走系统 PATH。"""
    monkeypatch.delenv("FFMPEG_BINARY", raising=False)
    monkeypatch.setattr(ft.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else "/usr/bin/ffprobe")
    monkeypatch.setattr(ft, "_resolve_builtin_ffmpeg", lambda: None)

    assert ft.resolve_ffmpeg() == "/usr/bin/ffmpeg"
    assert ft.resolve_ffprobe() == "/usr/bin/ffprobe"


def test_env_override_nonexistent_falls_back_to_path(monkeypatch):
    """env 指定但文件不存在 → 回退 PATH。"""
    monkeypatch.setenv("FFMPEG_BINARY", "/nonexistent/ffmpeg")
    monkeypatch.setattr(ft.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(ft, "_resolve_builtin_ffmpeg", lambda: None)

    assert ft.resolve_ffmpeg() == "/usr/bin/ffmpeg"


def test_builtin_ffmpeg(monkeypatch):
    """无 env 无 PATH → imageio-ffmpeg 内置 ffmpeg。"""
    monkeypatch.delenv("FFMPEG_BINARY", raising=False)
    monkeypatch.setattr(ft.shutil, "which", lambda name: None)
    monkeypatch.setattr(ft, "_resolve_builtin_ffmpeg", lambda: "/opt/venv/imageio_ffmpeg/bin/ffmpeg")

    assert ft.resolve_ffmpeg() == "/opt/venv/imageio_ffmpeg/bin/ffmpeg"


def test_builtin_ffprobe_sibling_found(monkeypatch, tmp_path):
    """ffmpeg 内置存在且同目录有 ffprobe → 返回兄弟程序。"""
    monkeypatch.delenv("FFPROBE_BINARY", raising=False)
    monkeypatch.setattr(ft.shutil, "which", lambda name: None)
    bin_dir = tmp_path
    fake_ff = bin_dir / "ffmpeg"
    fake_ff.write_text("x")
    fake_ffp = bin_dir / "ffprobe"
    fake_ffp.write_text("x")
    monkeypatch.setattr(ft, "_cache", {"ffmpeg": str(fake_ff)})

    assert ft.resolve_ffprobe() == str(fake_ffp)


def test_builtin_ffprobe_sibling_missing(monkeypatch, tmp_path):
    """ffmpeg 内置存在但无 ffprobe 兄弟 → ffprobe 返回 None。"""
    monkeypatch.delenv("FFPROBE_BINARY", raising=False)
    monkeypatch.setattr(ft.shutil, "which", lambda name: None)
    fake_ff = tmp_path / "ffmpeg"
    fake_ff.write_text("x")
    monkeypatch.setattr(ft, "_cache", {"ffmpeg": str(fake_ff)})

    assert ft.resolve_ffprobe() is None


def test_all_unavailable_returns_none(monkeypatch):
    """全部不可用 → None。"""
    monkeypatch.delenv("FFMPEG_BINARY", raising=False)
    monkeypatch.setattr(ft.shutil, "which", lambda name: None)
    monkeypatch.setattr(ft, "_resolve_builtin_ffmpeg", lambda: None)

    assert ft.resolve_ffmpeg() is None


def test_builtin_unavailable_exception(monkeypatch):
    """内置 imageio_ffmpeg 导入失败 → _resolve_builtin_ffmpeg 捕获并返回 None。"""
    monkeypatch.delenv("FFMPEG_BINARY", raising=False)
    monkeypatch.setattr(ft.shutil, "which", lambda name: None)

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "imageio_ffmpeg":
            raise ImportError("no builtin")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert ft._resolve_builtin_ffmpeg() is None
    assert ft.resolve_ffmpeg() is None


def test_cache_hit(monkeypatch):
    """进程级缓存：第二次调用不再重新解析。"""
    monkeypatch.delenv("FFMPEG_BINARY", raising=False)
    monkeypatch.setattr(ft.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(ft, "_resolve_builtin_ffmpeg", lambda: None)

    assert ft.resolve_ffmpeg() == "/usr/bin/ffmpeg"
    # 清空 PATH 解析，但缓存仍返回
    monkeypatch.setattr(ft.shutil, "which", lambda name: None)
    assert ft.resolve_ffmpeg() == "/usr/bin/ffmpeg"


# ══════════════════════════════════════════════════════════════════════
# Issue #78：裸 ffmpeg/ffprobe 调用统一收口
# ══════════════════════════════════════════════════════════════════════

class _R:
    """subprocess.run 的最小替身。"""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_resolve(monkeypatch, mapping):
    monkeypatch.setattr(ft, "resolve_binary", lambda name: mapping.get(name))


def test_resolve_cmd_binary_non_ffmpeg_untouched(monkeypatch):
    """首元素不是 ffmpeg 系 → 原样返回（不对未知命令做假设）。"""
    _patch_resolve(monkeypatch, {})
    cmd = ["python", "-c", "pass"]
    assert ft.resolve_cmd_binary(cmd) == cmd


def test_resolve_cmd_binary_empty():
    assert ft.resolve_cmd_binary([]) == []


def test_resolve_cmd_binary_replaces_head(monkeypatch):
    """裸 "ffmpeg" / 带路径的 ffmpeg.exe → 替换为解析后的绝对路径。"""
    _patch_resolve(monkeypatch, {"ffmpeg": "/usr/local/bin/ffmpeg"})
    assert ft.resolve_cmd_binary(["ffmpeg", "-y", "-i", "a.mp4"]) == [
        "/usr/local/bin/ffmpeg", "-y", "-i", "a.mp4",
    ]
    assert ft.resolve_cmd_binary(["/opt/bin/ffmpeg.exe", "-y"])[0] == "/usr/local/bin/ffmpeg"


def test_resolve_cmd_binary_missing_raises_i18n(monkeypatch):
    """解析不到可执行文件 → 抛 i18n 文案的 RuntimeError，而非裸 [WinError 2]。"""
    _patch_resolve(monkeypatch, {"ffmpeg": None})
    with pytest.raises(RuntimeError) as exc:
        ft.resolve_cmd_binary(["ffmpeg", "-y"])
    msg = str(exc.value)
    assert "ffmpeg" in msg and ("未找到" in msg or "No usable" in msg)


def test_probe_duration_missing_file_returns_default(tmp_path):
    assert ft.probe_duration(str(tmp_path / "nope.mp3"), default=0.0) == 0.0


def test_probe_duration_via_ffprobe(monkeypatch, tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    _patch_resolve(monkeypatch, {"ffprobe": "/usr/bin/ffprobe", "ffmpeg": None})
    monkeypatch.setattr(ft.subprocess, "run", lambda *a, **k: _R(stdout="12.5\n"))
    assert ft.probe_duration(str(f)) == 12.5


def test_probe_duration_ffprobe_missing_falls_back_to_ffmpeg(monkeypatch, tmp_path):
    """Docker 场景：无 ffprobe 但有 ffmpeg → 从 stderr Duration 解析，不再静默 0.0。"""
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    _patch_resolve(monkeypatch, {"ffprobe": None, "ffmpeg": "/usr/bin/ffmpeg"})
    monkeypatch.setattr(
        ft.subprocess, "run",
        lambda *a, **k: _R(stderr="  Duration: 00:00:07.25, start: 0.0, bitrate: 64 kb/s"),
    )
    assert ft.probe_duration(str(f)) == pytest.approx(7.25)


def test_probe_duration_both_missing_returns_default(monkeypatch, tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    _patch_resolve(monkeypatch, {"ffprobe": None, "ffmpeg": None})
    assert ft.probe_duration(str(f), default=3.0) == 3.0


def test_has_audio_stream_via_ffprobe(monkeypatch, tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    _patch_resolve(monkeypatch, {"ffprobe": "/usr/bin/ffprobe", "ffmpeg": None})
    monkeypatch.setattr(ft.subprocess, "run", lambda *a, **k: _R(stdout="audio\n"))
    assert ft.has_audio_stream(str(f)) is True


def test_has_audio_stream_ffprobe_missing_falls_back_to_ffmpeg(monkeypatch, tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    _patch_resolve(monkeypatch, {"ffprobe": None, "ffmpeg": "/usr/bin/ffmpeg"})
    monkeypatch.setattr(
        ft.subprocess, "run",
        lambda *a, **k: _R(stderr="  Stream #0:1(und): Audio: aac (LC), 44100 Hz, mono"),
    )
    assert ft.has_audio_stream(str(f)) is True


def test_has_audio_stream_both_missing_false(monkeypatch, tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    _patch_resolve(monkeypatch, {"ffprobe": None, "ffmpeg": None})
    assert ft.has_audio_stream(str(f)) is False


def test_probe_video_dimensions_via_ffprobe(monkeypatch, tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    _patch_resolve(monkeypatch, {"ffprobe": "/usr/bin/ffprobe", "ffmpeg": None})
    monkeypatch.setattr(
        ft.subprocess, "run",
        lambda *a, **k: _R(stdout='{"streams":[{"codec_type":"video","width":768,"height":1152}]}'),
    )
    assert ft.probe_video_dimensions(str(f)) == (768, 1152)


def test_probe_video_dimensions_ffmpeg_fallback(monkeypatch, tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    _patch_resolve(monkeypatch, {"ffprobe": None, "ffmpeg": "/usr/bin/ffmpeg"})
    monkeypatch.setattr(
        ft.subprocess, "run",
        lambda *a, **k: _R(stderr="  Stream #0:0(und): Video: h264, yuv420p, 640x360 [SAR 1:1]"),
    )
    assert ft.probe_video_dimensions(str(f)) == (640, 360)


def test_probe_video_dimensions_unavailable(monkeypatch, tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    _patch_resolve(monkeypatch, {"ffprobe": None, "ffmpeg": None})
    assert ft.probe_video_dimensions(str(f)) == (None, None)


# ══════════════════════════════════════════════════════════════════════
# Issue #78 崩溃点：SilentTTSEngine 无 ffmpeg 时应给 i18n 报错
# ══════════════════════════════════════════════════════════════════════

def test_silent_tts_raises_i18n_when_ffmpeg_missing(monkeypatch, tmp_path):
    """此前裸 "ffmpeg" → Windows 上抛 [WinError 2]；现在抛可读的 i18n 错误。"""
    import asyncio

    import core.audio.tts as tts_mod

    monkeypatch.setattr(tts_mod, "resolve_binary", lambda name: None)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(
            tts_mod.SilentTTSEngine().generate(
                "测试", str(tmp_path / "s.mp3"), duration_sec=1.0
            )
        )
    msg = str(exc.value)
    assert "ffmpeg" in msg and ("未找到" in msg or "No usable" in msg)


def test_silent_tts_uses_resolved_path(monkeypatch, tmp_path):
    """有 ffmpeg 时用解析后的绝对路径起子进程，不再依赖 PATH。"""
    import asyncio

    import core.audio.tts as tts_mod

    monkeypatch.setattr(tts_mod, "resolve_binary", lambda name: "/custom/ffmpeg")
    captured = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    path, cues = asyncio.run(
        tts_mod.SilentTTSEngine().generate("测试", str(tmp_path / "s.mp3"), duration_sec=1.0)
    )
    assert captured["args"][0] == "/custom/ffmpeg"
    assert cues is None and path.endswith("s.mp3")

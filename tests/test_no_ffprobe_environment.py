"""回归：无 ffprobe 环境（Docker 镜像只随 ffmpeg）下的探测与 concat 快路径。

背景
----
``Dockerfile`` 只把 imageio-ffmpeg 的静态 ``ffmpeg`` 软链进 PATH，**不提供
``ffprobe``**，因此 ``resolve_binary("ffprobe")`` 在容器里恒为 ``None``。
三处调用方此前把该返回值直接塞进 ``subprocess.run([...])`` 命令列表：

1. ``ConcatMixin._try_ffmpeg_copy_concat`` → ``TypeError`` 被 ``except Exception``
   吞掉 → 返回 False → **2.1a ``-c copy`` 快路径在 Docker 里从未生效**，
   每次拼接都退化成 moviepy 全量重编码；
2. ``ConcatMixin._get_duration`` → 同上 → **恒返回 0.0**，音视频时长比较与
   尾部补齐基于错误的 0 计算；
3. ``AudioOverlayMixin._get_video_size`` → 同上 → **恒回退竖屏 (768, 1152)**，
   1280×720 横屏任务的 SRT→ASS 字幕画布按竖屏计算，字号与定位整体偏小。

本文件用**真实素材 + 真实 ffmpeg**，仅屏蔽 ffprobe，锁定修复后的行为。
CI（apt 装了 ffmpeg 与 ffprobe）不会覆盖这一场景，故单列成组。
"""
import os
import subprocess

import pytest

import core.compositor.ffmpeg_tool as ft
from core.compositor.concatenator.audio_overlay import AudioOverlayMixin
from core.compositor.concatenator.concat import VideoConcatenator

ASSET = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "mock_regression", "assets", "test_video_5s.mp4",
)
_HAS_ASSET = os.path.exists(ASSET)

# 真实 ffmpeg 调用（生成横屏素材 / 拼接编码）→ 慢速集成测试，CI 全量执行
pytestmark = pytest.mark.slow


@pytest.fixture
def no_ffprobe(monkeypatch):
    """屏蔽 ffprobe，保留真实的 ffmpeg 解析（复刻 Docker 镜像环境）。"""
    real = ft.resolve_binary

    def _fake(name):
        return None if name == "ffprobe" else real(name)

    ft._cache.clear()
    monkeypatch.setattr(ft, "resolve_binary", _fake)
    yield
    ft._cache.clear()


def _make_landscape(tmp_path, name="landscape.mp4", seconds=2):
    """用解析到的 ffmpeg 生成一段 1280×720 横屏素材（无 ffprobe 参与）。"""
    out = os.path.join(str(tmp_path), name)
    ffmpeg = ft.resolve_binary("ffmpeg")
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i",
         f"testsrc=size=1280x720:rate=30:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
    )
    return out


@pytest.mark.skipif(not _HAS_ASSET, reason="mock 素材缺失")
def test_fast_concat_signature_probe_without_ffprobe(no_ffprobe, tmp_path):
    """无 ffprobe：仍能探测出片段签名，分辨率/帧率一致 → 走 -c copy 快路径。"""
    a, b = str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")
    for dst in (a, b):
        with open(ASSET, "rb") as src, open(dst, "wb") as out:
            out.write(src.read())

    assert ft.probe_video_signature(a) == (768, 1152, "30")
    assert VideoConcatenator._try_ffmpeg_copy_concat([a, b], str(tmp_path / "out.mp4"))


@pytest.mark.skipif(not _HAS_ASSET, reason="mock 素材缺失")
def test_fast_concat_mismatched_clips_still_skipped(no_ffprobe, tmp_path):
    """分辨率不一致仍应安全放弃快路径（不因兜底探测而误判为一致）。"""
    portrait = str(tmp_path / "portrait.mp4")
    with open(ASSET, "rb") as src, open(portrait, "wb") as out:
        out.write(src.read())
    landscape = _make_landscape(tmp_path)

    assert VideoConcatenator._try_ffmpeg_copy_concat(
        [portrait, landscape], str(tmp_path / "out.mp4")
    ) is False


@pytest.mark.skipif(not _HAS_ASSET, reason="mock 素材缺失")
def test_get_duration_without_ffprobe(no_ffprobe):
    """无 ffprobe：时长走 ffmpeg -i 兜底，不再恒为 0.0。"""
    assert VideoConcatenator._get_duration(ASSET) == pytest.approx(5.0, abs=0.2)


def test_get_video_size_without_ffprobe(no_ffprobe, tmp_path):
    """无 ffprobe：横屏素材读到真实 1280×720，而非恒定竖屏回退 (768, 1152)。"""
    landscape = _make_landscape(tmp_path)
    assert os.path.exists(landscape) and os.path.getsize(landscape) > 0
    assert AudioOverlayMixin._get_video_size(landscape) == (1280, 720)


def test_no_ffprobe_fixture_matches_docker(no_ffprobe):
    """自检：fixture 确实复刻了「有 ffmpeg、无 ffprobe」的容器环境。"""
    assert ft.resolve_binary("ffprobe") is None
    assert ft.resolve_binary("ffmpeg")

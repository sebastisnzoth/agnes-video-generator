"""core.compositor.music_timeline 测试（v7.1 音乐视频）。

- 纯函数：分段规则（N = ceil(T/10)、末段余数、浮点抖动、非法参数）；
- ffmpeg 相关：定长时间轴的实际时长 / 分辨率 / 无音轨，歌曲转码为 mp3。
  无 ffmpeg 时自动跳过（CI 与本地均安装了静态 ffmpeg 时全部执行）。

用法:
    .venv/bin/python -m pytest tests/test_music_video_timeline.py -q
"""

import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from core.compositor.ffmpeg_tool import (  # noqa: E402
    has_audio_stream,
    probe_duration,
    probe_video_dimensions,
    resolve_binary,
)
from core.compositor.music_timeline import (  # noqa: E402
    build_timed_video_track,
    plan_uniform_spans,
    transcode_song_to_mp3,
)

needs_ffmpeg = pytest.mark.skipif(resolve_binary("ffmpeg") is None, reason="ffmpeg not available")


# ═══════════════════════════════════════════════════
# 纯函数：分段
# ═══════════════════════════════════════════════════

class TestPlanUniformSpans:
    def test_three_minutes_gives_eighteen_full_spans(self):
        spans = plan_uniform_spans(180.0, 10)
        assert len(spans) == 18
        assert all(end - start == pytest.approx(10.0) for start, end in spans)
        assert spans[-1][1] == 180.0

    def test_remainder_becomes_last_span(self):
        spans = plan_uniform_spans(185.5, 10)
        assert len(spans) == 19
        assert spans[-1] == [180.0, 185.5]

    def test_exact_multiple_has_no_extra_span(self):
        assert plan_uniform_spans(30.0, 10) == [[0.0, 10.0], [10.0, 20.0], [20.0, 30.0]]

    def test_short_song_is_one_span(self):
        assert plan_uniform_spans(3.2, 10) == [[0.0, 3.2]]

    def test_float_jitter_does_not_add_a_span(self):
        assert len(plan_uniform_spans(20.0000000001, 10)) == 2

    def test_five_minutes_gives_thirty_spans(self):
        assert len(plan_uniform_spans(300.0, 10)) == 30

    @pytest.mark.parametrize("total", [10.0001, 47.3, 123.456, 299.999])
    def test_spans_are_contiguous_and_cover_song(self, total):
        spans = plan_uniform_spans(total, 10)
        assert len(spans) == math.ceil(total / 10)
        assert spans[0][0] == 0.0
        assert spans[-1][1] == pytest.approx(total, abs=1e-3)
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
            assert prev_end == pytest.approx(next_start)

    @pytest.mark.parametrize("total, clip", [(0.0, 10), (10.0, 0), (-1.0, 10)])
    def test_rejects_non_positive_arguments(self, total, clip):
        with pytest.raises(ValueError):
            plan_uniform_spans(total, clip)


# ═══════════════════════════════════════════════════
# 参数校验（不需要 ffmpeg）
# ═══════════════════════════════════════════════════

class TestBuildTimedValidation:
    def test_clip_span_count_mismatch_raises(self, tmp_path):
        with pytest.raises(ValueError, match="mismatch"):
            build_timed_video_track(["a.mp4"], [[0.0, 1.0], [1.0, 2.0]], str(tmp_path / "o.mp4"), 320, 240)

    def test_non_positive_span_raises(self, tmp_path):
        with pytest.raises(ValueError, match="non-positive"):
            build_timed_video_track(["a.mp4"], [[2.0, 2.0]], str(tmp_path / "o.mp4"), 320, 240)

    def test_missing_clip_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            build_timed_video_track([str(tmp_path / "nope.mp4")], [[0.0, 1.0]], str(tmp_path / "o.mp4"), 320, 240)

    def test_missing_song_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            transcode_song_to_mp3(str(tmp_path / "nope.wav"), str(tmp_path / "song.mp3"))


# ═══════════════════════════════════════════════════
# ffmpeg 集成
# ═══════════════════════════════════════════════════

def _run(cmd):
    subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=120)


def _make_clip(path, seconds, width=160, height=120):
    """生成一段真实的 H.264 测试视频（带 testsrc 画面，无音轨）。"""
    _run([
        resolve_binary("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=25",
        "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ])
    return str(path)


def _make_tone(path, seconds, ext="wav"):
    codec = ["-c:a", "pcm_s16le"] if ext == "wav" else ["-c:a", "libmp3lame", "-q:a", "2"]
    _run([
        resolve_binary("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        *codec, str(path),
    ])
    return str(path)


@needs_ffmpeg
class TestBuildTimedVideoTrack:
    def test_output_matches_span_total_and_has_no_audio(self, tmp_path):
        clips = [
            _make_clip(tmp_path / "c0.mp4", 1.2),   # 比跨度短 → 冻结尾帧补齐
            _make_clip(tmp_path / "c1.mp4", 0.6),
            _make_clip(tmp_path / "c2.mp4", 2.0),   # 比跨度长 → 裁剪
        ]
        spans = [[0.0, 2.0], [2.0, 3.5], [3.5, 3.9]]
        out = str(tmp_path / "timeline.mp4")

        build_timed_video_track(clips, spans, out, 320, 240)

        assert probe_duration(out) == pytest.approx(3.9, abs=0.06)
        assert probe_video_dimensions(out) == (320, 240)
        assert has_audio_stream(out) is False

    def test_song_length_track_is_not_truncated_by_last_span(self, tmp_path):
        # 整首歌 6.2 秒，分三段：确认末段精确结束于 6.2 秒（不多不少）
        clips = [_make_clip(tmp_path / f"c{i}.mp4", 1.0) for i in range(3)]
        spans = [[0.0, 2.5], [2.5, 4.0], [4.0, 6.2]]
        out = str(tmp_path / "timeline.mp4")
        build_timed_video_track(clips, spans, out, 160, 120)
        assert probe_duration(out) == pytest.approx(6.2, abs=0.06)


@needs_ffmpeg
class TestTranscodeSong:
    def test_wav_is_converted_to_mp3_with_same_duration(self, tmp_path):
        src = _make_tone(tmp_path / "tone.wav", 2.0, ext="wav")
        dst = str(tmp_path / "song.mp3")

        assert transcode_song_to_mp3(src, dst) == dst

        assert os.path.getsize(dst) > 0
        assert probe_duration(dst) == pytest.approx(2.0, abs=0.1)
        assert has_audio_stream(dst) is True

"""core.compositor.music_timeline — 音乐视频定长时间轴（v7.1）。

问题背景：Agnes 返回的「10 秒」片段实际约 10.04 秒（241 帧 / 24 fps）。若直接拼接，
30 段后累计约 1.2 秒漂移，歌词字幕与画面节奏对不上。因此按精确浮点跨度逐段裁剪/补帧，
一次编码输出与歌曲等长的无声时间轴，再交给 ``concat_videos_with_audio_overlay`` 叠加音频。

设计依据：docs/plans/v7.1/system_design.md §四.5。
"""

import logging
import math
import os
import subprocess
from typing import List, Sequence

from core.compositor.ffmpeg_tool import resolve_binary

logger = logging.getLogger(__name__)

_FPS = 24
_TIMEOUT_SECONDS = 1800


def plan_uniform_spans(total: float, clip: int) -> List[List[float]]:
    """把总时长切成等长段（最后一段为余数）。

    Args:
        total: 歌曲总时长（秒），必须 > 0。
        clip: 每段目标时长（秒），必须 > 0。

    Returns:
        ``[[start, end], ...]``，首尾相接，末段结束于 ``total``。
        段数 ``N = ceil(total / clip)``（对 1e-6 秒量级的浮点误差做了容忍）。

    Raises:
        ValueError: 参数非正数。
    """
    if total <= 0 or clip <= 0:
        raise ValueError(f"total and clip must be positive (total={total}, clip={clip})")

    n = max(1, math.ceil(round(total / clip, 6)))
    spans: List[List[float]] = []
    for i in range(n):
        start = round(i * clip, 3)
        end = round(min((i + 1) * clip, total), 3)
        if i == n - 1:
            end = round(total, 3)
        spans.append([start, end])
    return spans


def _segment_filter(index: int, width: int, height: int, duration: float) -> str:
    """单段滤镜：统一帧率与尺寸，补帧到足够长后裁剪为精确时长。"""
    d = f"{duration:.3f}"
    return (
        f"[{index}:v]fps={_FPS},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        f"tpad=stop_mode=clone:stop_duration={d},"
        f"trim=duration={d},setpts=PTS-STARTPTS[v{index}]"
    )


def build_timed_video_track(
    clip_paths: Sequence[str],
    spans: Sequence[Sequence[float]],
    output_path: str,
    width: int,
    height: int,
) -> str:
    """把各段视频裁剪/补帧到 ``spans`` 指定的精确时长后拼接为无声视频。

    Args:
        clip_paths: 各段视频路径（顺序与 ``spans`` 对应）。
        spans: 各段 ``[start, end]``；时长 = end - start。
        output_path: 输出 MP4 路径。
        width / height: 输出分辨率（各段会被等比缩放并居中填充）。

    Returns:
        输出文件路径。

    Raises:
        ValueError: 段数不一致或某段时长非正。
        FileNotFoundError: 某段视频不存在。
        RuntimeError: ffmpeg 不可用或执行失败。
    """
    if not clip_paths or len(clip_paths) != len(spans):
        raise ValueError(f"clip/span count mismatch: {len(clip_paths)} clips, {len(spans)} spans")

    durations = [float(end) - float(start) for start, end in spans]
    if any(d <= 0 for d in durations):
        raise ValueError(f"non-positive span duration: {durations}")
    for path in clip_paths:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise FileNotFoundError(f"clip missing or empty: {path}")

    ffmpeg = resolve_binary("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg binary not found")

    n = len(clip_paths)
    graph = ";".join(_segment_filter(i, width, height, durations[i]) for i in range(n))
    graph += ";" + "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[outv]"

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for path in clip_paths:
        cmd += ["-i", path]
    cmd += [
        "-filter_complex", graph,
        "-map", "[outv]",
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output_path,
    ]

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    logger.info(f"[MusicVideo] building timed track: {n} segments, {sum(durations):.2f}s → {output_path}")
    proc = subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-800:]
        raise RuntimeError(f"timed track ffmpeg failed (code={proc.returncode}): {tail}")
    return output_path


def transcode_song_to_mp3(src: str, dst: str) -> str:
    """把用户上传的歌曲统一转码为任务目录下的 ``song.mp3``（仅保留第一条音频流）。

    统一为 mp3 后产物名固定，便于产物清单与续传；``-q:a 2`` 为高质量 VBR，
    歌曲本身是有损或无损源均可接受。

    Raises:
        FileNotFoundError: 源文件不存在。
        RuntimeError: ffmpeg 不可用、解码失败或输出为空。
    """
    if not os.path.exists(src):
        raise FileNotFoundError(f"song file missing: {src}")
    ffmpeg = resolve_binary("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg binary not found")

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-vn", "-map", "0:a:0",
        "-c:a", "libmp3lame", "-q:a", "2",
        dst,
    ]
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=600)
    if proc.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-800:]
        raise RuntimeError(f"song transcode failed (code={proc.returncode}): {tail}")
    return dst

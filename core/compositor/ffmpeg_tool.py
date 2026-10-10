"""core.compositor.ffmpeg_tool — ffmpeg / ffprobe 可执行文件统一解析。

背景：此前所有 ffmpeg/ffprobe 调用都用裸命令字符串经 ``subprocess.run`` 执行，
依赖系统 PATH 搜索可执行文件。Windows 未安装 ffmpeg 时 ``CreateProcess`` 抛
``[WinError 2] The system cannot find the file specified``，且发生在任务运行到
拼接步骤而非启动时（见 Issue #36 的完整 traceback）。

解析优先级（推荐，但不强制用户安装系统 ffmpeg）：
  1. 环境变量显式指定：``FFMPEG_BINARY`` / ``FFPROBE_BINARY`` —— 尊重用户意图
  2. 系统 PATH（``shutil.which``）—— 已安装则用之，绝不覆盖
  3. ``imageio-ffmpeg`` 内置静态二进制兜底 —— wheel 自带，无需用户操作

结果带进程级缓存；全部不可用时返回 None，由启动检测 / 调用方给出清晰指引
而非裸的 ``[WinError 2]``。
"""
import logging
import os
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

# 二进制名 → 覆盖环境变量名
_EXE_OVERRIDE = {"ffmpeg": "FFMPEG_BINARY", "ffprobe": "FFPROBE_BINARY"}
_cache: "dict[str, str | None]" = {}


def resolve_binary(name: str) -> "str | None":
    """按优先级解析 ffmpeg/ffprobe 可执行文件绝对路径，进程内缓存。

    Args:
        name: ``"ffmpeg"`` 或 ``"ffprobe"``。

    Returns:
        可用可执行文件绝对路径；全部不可用时返回 None。
    """
    if name not in _EXE_OVERRIDE:
        raise ValueError(f"unknown binary: {name}")
    if name in _cache:
        return _cache[name]
    path = _resolve(name)
    _cache[name] = path
    return path


def resolve_ffmpeg() -> "str | None":
    """便捷：解析 ffmpeg。"""
    return resolve_binary("ffmpeg")


def resolve_ffprobe() -> "str | None":
    """便捷：解析 ffprobe（可能为 None，仅时长/尺寸探测用，调用方自带兜底）。"""
    return resolve_binary("ffprobe")


def probe_duration(path: str, default: float = 0.0) -> float:
    """探测媒体时长（秒），三级兜底，**永不抛异常**。

    背景（Issue #78）：此前多处探测用裸 ``["ffprobe", ...]`` 直接交给
    ``subprocess.run``，在只带 imageio-ffmpeg 内置二进制（无 ffprobe）的环境里
    要么抛异常被静默吞掉、要么把 ``None`` 塞进命令行，最终统一降级为 0.0/None，
    连「ffmpeg 可用时的兜底探测」也没有。此处按可用性依次尝试：

    1. ``ffprobe``（解析到真实路径时才用）；
    2. ``ffmpeg -i`` 的 stderr ``Duration:`` 行（ffmpeg 为硬依赖，必有兜底）；
    3. 调用方给定的 ``default``。

    Args:
        path: 媒体文件路径。
        default: 全部探测失败时返回的兜底值（秒）。

    Returns:
        时长（秒）；无法探测时返回 ``default``。
    """
    if not path or not os.path.exists(path):
        return default

    ffprobe = resolve_binary("ffprobe")
    if ffprobe:
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
            )
            val = float((r.stdout or "").strip() or 0)
            if val > 0:
                return val
        except Exception as e:
            logger.warning(f"[Compositor] ffprobe duration failed: {e}")

    ffmpeg = resolve_binary("ffmpeg")
    if ffmpeg:
        try:
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", path],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
            )
            m = re.search(
                r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or ""
            )
            if m:
                h, mi, s = m.groups()
                val = int(h) * 3600 + int(mi) * 60 + float(s)
                if val > 0:
                    logger.info(
                        f"[Compositor] duration via ffmpeg fallback: {val:.2f}s ({path})"
                    )
                    return val
        except Exception as e:
            logger.warning(f"[Compositor] ffmpeg duration probe failed: {e}")

    logger.warning(f"[Compositor] duration probe unavailable for {path}, using default {default}")
    return default


def has_audio_stream(path: str) -> bool:
    """检测媒体容器是否含音频流（Issue #78：不再依赖裸 ffprobe）。

    优先 ``ffprobe -select_streams a``；ffprobe 不可用时回退解析
    ``ffmpeg -i`` 的 stderr 流信息（``Stream #0:1: Audio: ...``）。
    两者都不可用或探测异常时返回 ``False``（调用方按「无音频」保守处理）。

    Args:
        path: 媒体文件路径。

    Returns:
        含音频流返回 True。
    """
    if not path or not os.path.exists(path):
        return False

    ffprobe = resolve_binary("ffprobe")
    if ffprobe:
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                return "audio" in (r.stdout or "")
        except Exception as e:
            logger.warning(f"[Compositor] ffprobe audio-stream probe failed: {e}")

    ffmpeg = resolve_binary("ffmpeg")
    if ffmpeg:
        try:
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", path],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
            )
            if re.search(r"Stream #\S+.*?: Audio:", r.stderr or ""):
                return True
        except Exception as e:
            logger.warning(f"[Compositor] ffmpeg audio-stream probe failed: {e}")

    return False


def probe_video_dimensions(path: str) -> "tuple[int | None, int | None]":
    """探测视频宽高（像素），失败返回 ``(None, None)``（Issue #78）。

    优先 ``ffprobe`` JSON 输出；ffprobe 不可用时回退解析 ``ffmpeg -i``
    stderr 里的 ``768x1152`` 分辨率描述。
    """
    if not path or not os.path.exists(path):
        return None, None

    ffprobe = resolve_binary("ffprobe")
    if ffprobe:
        try:
            r = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_streams", path],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
            )
            import json
            info = json.loads(r.stdout or "{}")
            for s in info.get("streams", []):
                if s.get("codec_type") == "video":
                    return s.get("width"), s.get("height")
        except Exception as e:
            logger.warning(f"[Compositor] ffprobe size probe failed: {e}")

    ffmpeg = resolve_binary("ffmpeg")
    if ffmpeg:
        try:
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", path],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
            )
            m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", r.stderr or "")
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception as e:
            logger.warning(f"[Compositor] ffmpeg size probe failed: {e}")

    logger.warning(f"[Compositor] cannot detect dimensions: {path}")
    return None, None


def probe_video_signature(path: str) -> "tuple[int | None, int | None, str | None]":
    """探测视频的 ``(宽, 高, 平均帧率)`` 签名，供 concat demuxer 快路径比对。

    与 :func:`probe_duration` / :func:`probe_video_dimensions` 同一套兜底策略：
    优先 ``ffprobe``（一次拿到宽高与帧率），ffprobe 不可用时回退解析
    ``ffmpeg -i`` 的 stderr。两者都不可用时返回 ``(None, None, None)``，
    由调用方决定回退策略（concat 快路径即应安全放弃）。

    .. note::
       本函数存在的直接原因：``-c copy`` 快路径此前直接把
       ``resolve_binary("ffprobe")`` 塞进命令列表，在只有 ffmpeg、没有
       ffprobe 的环境（**Docker 镜像**即如此——Dockerfile 只把 imageio-ffmpeg
       的 ffmpeg 软链到 PATH）会抛 ``TypeError: expected str, bytes or
       os.PathLike object, not NoneType``，被 ``except Exception`` 吞掉后
       静默回退 moviepy 全量重编码，2.1a 快路径在 Docker 里从未生效。

    Args:
        path: 视频文件路径。

    Returns:
        ``(width, height, avg_frame_rate)``；任一项探测不到即为 None。
        帧率在两条路径下格式不同（ffprobe ``30/1``，ffmpeg 回退 ``30``），
        但同一次比对内取值方式一致，不影响签名比对语义。
    """
    if not path or not os.path.exists(path):
        return None, None, None

    ffprobe = resolve_binary("ffprobe")
    if ffprobe:
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,avg_frame_rate",
                 "-of", "csv=s=x:p=0", path],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
            )
            parts = (r.stdout or "").strip().split("x")
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                fps = parts[2].strip()
                # 0/0 = 容器未记录帧率，按「未知」处理，交回退路径解析
                return int(parts[0]), int(parts[1]), (None if fps in ("", "0/0") else fps)
        except Exception as e:
            logger.warning(f"[Compositor] ffprobe signature probe failed: {e}")

    ffmpeg = resolve_binary("ffmpeg")
    if ffmpeg:
        try:
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", path],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
            )
            err = r.stderr or ""
            m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
            if m:
                fps_m = re.search(r"(\d+(?:\.\d+)?)\s+fps", err)
                return int(m.group(1)), int(m.group(2)), (fps_m.group(1) if fps_m else None)
        except Exception as e:
            logger.warning(f"[Compositor] ffmpeg signature probe failed: {e}")

    logger.warning(f"[Compositor] video signature unavailable: {path}")
    return None, None, None


def _resolve(name: str) -> "str | None":
    # 1) 显式指定
    override = os.environ.get(_EXE_OVERRIDE[name])
    if override and os.path.exists(override):
        logger.info(f"[Compositor] {name}: explicit {override}")
        return override

    # 2) 系统 PATH
    found = shutil.which(name)
    if found:
        logger.info(f"[Compositor] {name}: system {found}")
        return found

    # 3) imageio-ffmpeg 内置（仅自带 ffmpeg；ffprobe 从同目录推导）
    base = _cache.get("ffmpeg") or _resolve_builtin_ffmpeg()
    if base:
        if name == "ffmpeg":
            return base
        probe = _sibling(base, "ffprobe")
        if probe:
            logger.info(f"[Compositor] {name}: builtin {probe}")
            return probe

    logger.warning(f"[Compositor] {name}: not found (no system PATH nor builtin)")
    return None


def _resolve_builtin_ffmpeg() -> "str | None":
    """取 imageio-ffmpeg 内置静态二进制；加载/失败时记录并返回 None。"""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # 加载失败 / 内置缺失 / 首次下载失败
        logger.warning(f"[Compositor] builtin ffmpeg unavailable: {e}")
        return None
    if exe and os.path.exists(exe):
        logger.info(f"[Compositor] ffmpeg: builtin {exe}")
        return exe
    return None


def _sibling(exe: str, stem: str) -> "str | None":
    """由可执行文件路径推导同目录兄弟程序（Windows 补 .exe）。"""
    candidate = os.path.join(
        os.path.dirname(exe), stem + (".exe" if os.name == "nt" else "")
    )
    return candidate if os.path.exists(candidate) else None


def resolve_cmd_binary(cmd: list) -> list:
    """把命令列表首元素里的裸 ``ffmpeg`` / ``ffprobe`` 替换为解析后的绝对路径。

    Issue #78：多处调用方按 ``["ffmpeg", "-y", ...]`` 拼命令，未统一走
    ``resolve_binary``。传入本函数后：

    - 首元素是 ``ffmpeg`` / ``ffprobe``（含带路径后缀的写法）→ 替换为真实路径；
    - 解析不到可执行文件 → 抛 ``RuntimeError``（i18n 文案），而非在下游
      抛不可读的 ``[WinError 2]`` / ``FileNotFoundError``；
    - 其他命令（首元素非 ffmpeg 系）原样返回，不做任何假设。

    Args:
        cmd: 原始命令列表。

    Returns:
        处理后的命令列表（新列表，不修改入参）。
    """
    if not cmd:
        return cmd
    head = os.path.basename(str(cmd[0]))
    stem = os.path.splitext(head)[0].lower()
    if stem not in _EXE_OVERRIDE:
        return cmd
    resolved = resolve_binary(stem)
    if not resolved:
        from core.i18n_backend import translate

        raise RuntimeError(translate("error.ffmpeg_missing"))
    return [resolved, *cmd[1:]]
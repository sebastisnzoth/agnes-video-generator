"""core.audio.lyrics — 歌词自动识别（v7.1 音乐视频）。

基于 faster-whisper（CTranslate2 实现的 Whisper，CPU int8 推理，**不依赖 PyTorch**）。
流程：词级时间戳 → ``group_words_into_lines`` 切行 → ``lyric_lines_to_srt`` 输出 SRT。

设计依据：docs/plans/v7.1/music_video_PRD.md §三.2、§五。
降级约定：依赖缺失 / 模型加载失败抛 ``LyricsUnavailableError``；调用方据此跳过字幕。
"""

import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, List, Optional

import srt

logger = logging.getLogger(__name__)

DEFAULT_LYRICS_MODEL = "small"

# 幻觉抑制：伴奏段落常被 Whisper 识别为虚假文本，按片段置信度过滤
_NO_SPEECH_THRESHOLD = 0.6
_AVG_LOGPROB_THRESHOLD = -1.0

# 分行规则
_LINE_MAX_GAP = 0.8        # 词间隔超过该秒数则换行
_LINE_MAX_CHARS = 36       # 单行字符数上限（不计空格）
_LINE_MAX_WORDS = 10       # 单行词数上限
_LINE_MIN_DURATION = 0.6   # 短于该时长的行并入前一行


class LyricsUnavailableError(RuntimeError):
    """歌词识别不可用（依赖未安装、模型无法加载）。调用方应降级为无字幕。"""


@dataclass(frozen=True)
class WordTiming:
    """单个词的时间戳（秒）。"""

    start: float
    end: float
    word: str
    probability: float = 1.0


@dataclass(frozen=True)
class LyricLine:
    """一行歌词（秒，时间轴即歌曲时间轴）。"""

    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


# ═══════════════════════════════════════════════════════════════
# 纯函数：切行与 SRT（可单测，不依赖模型）
# ═══════════════════════════════════════════════════════════════

_ASCII_WORD_EDGE = re.compile(r"[A-Za-z0-9]")


def _needs_space(prev: str, nxt: str) -> bool:
    """相邻两个 token 之间是否需要空格：仅拉丁字母/数字之间加空格（CJK 直接相连）。"""
    return bool(_ASCII_WORD_EDGE.match(prev[-1:]) and _ASCII_WORD_EDGE.match(nxt[:1]))


def _join_tokens(tokens: Iterable[str]) -> str:
    out = ""
    for tok in tokens:
        if out and _needs_space(out, tok):
            out += " "
        out += tok
    return out


def _visible_len(text: str) -> int:
    return len(text.replace(" ", ""))


def group_words_into_lines(
    words: Iterable[WordTiming],
    *,
    max_gap: float = _LINE_MAX_GAP,
    max_chars: int = _LINE_MAX_CHARS,
    max_words: int = _LINE_MAX_WORDS,
    min_duration: float = _LINE_MIN_DURATION,
) -> List[LyricLine]:
    """按停顿、字数、词数把词级时间戳切成歌词行。

    规则：词间隔 > ``max_gap``、或当前行已达 ``max_words`` 词、或加入后超过
    ``max_chars`` 字符时换行；时长不足 ``min_duration`` 的行并入前一行；
    最终保证行按时间单调、相邻行不重叠。
    """
    ordered = sorted((w for w in words if w.word and w.word.strip()), key=lambda w: w.start)

    # 第一遍：按停顿/长度切行。每行为 [start, end, tokens]
    raw: List[list] = []
    for w in ordered:
        token = w.word.strip()
        if raw:
            cur = raw[-1]
            gap = w.start - cur[1]
            too_long = _visible_len(_join_tokens(cur[2] + [token])) > max_chars
            if gap > max_gap or len(cur[2]) >= max_words or too_long:
                raw.append([w.start, w.end, [token]])
            else:
                cur[1] = max(cur[1], w.end)
                cur[2].append(token)
        else:
            raw.append([w.start, w.end, [token]])

    # 第二遍：过短行并入前一行（首行即使过短也保留）
    merged: List[list] = []
    for start, end, tokens in raw:
        if merged and (end - start) < min_duration:
            prev = merged[-1]
            prev[1] = max(prev[1], end)
            prev[2].extend(tokens)
        else:
            merged.append([start, end, list(tokens)])

    # 第三遍：消除相邻行重叠（以后一行开始为准截断前一行，且不早于其开始）
    lines: List[LyricLine] = []
    for i, (start, end, tokens) in enumerate(merged):
        if i + 1 < len(merged):
            next_start = merged[i + 1][0]
            if end > next_start > start:
                end = next_start
        lines.append(LyricLine(start=float(start), end=float(max(end, start)), text=_join_tokens(tokens)))
    return lines


def lyric_lines_to_srt(lines: Iterable[LyricLine]) -> str:
    """把歌词行输出为 SubRip 文本；无行时返回空字符串。"""
    items = [
        srt.Subtitle(
            index=i,
            start=timedelta(seconds=line.start),
            end=timedelta(seconds=line.end),
            content=line.text,
        )
        for i, line in enumerate(lines, start=1)
    ]
    return srt.compose(items) if items else ""


# ═══════════════════════════════════════════════════════════════
# 转写（依赖 faster-whisper；懒加载）
# ═══════════════════════════════════════════════════════════════

_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: dict = {}


def get_lyrics_model_name() -> str:
    """模型名：环境变量 ``AGNES_LYRICS_MODEL`` 优先，缺省 ``small``。"""
    return (os.getenv("AGNES_LYRICS_MODEL") or "").strip() or DEFAULT_LYRICS_MODEL


def _load_model(name: str):
    """加载并缓存 WhisperModel（进程内单例，按模型名区分）。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover - 取决于安装环境
        raise LyricsUnavailableError("faster-whisper is not installed") from e

    with _MODEL_LOCK:
        model = _MODEL_CACHE.get(name)
        if model is None:
            try:
                model = WhisperModel(name, device="cpu", compute_type="int8")
            except Exception as e:
                raise LyricsUnavailableError(f"cannot load whisper model {name!r}: {e}") from e
            _MODEL_CACHE[name] = model
    return model


def transcribe_words(audio_path: str, model_name: Optional[str] = None) -> List[WordTiming]:
    """识别歌曲中的词级时间戳（阻塞调用，应在线程中执行）。

    过滤 ``no_speech_prob`` 过高或 ``avg_logprob`` 过低的片段（伴奏幻觉）。
    """
    model = _load_model(model_name or get_lyrics_model_name())
    segments, _info = model.transcribe(
        audio_path,
        word_timestamps=True,
        vad_filter=False,  # 关闭 VAD：避免切掉轻声演唱，幻觉由下方过滤兜底
        condition_on_previous_text=False,
        beam_size=5,
    )
    words: List[WordTiming] = []
    dropped = 0
    for seg in segments:
        if seg.no_speech_prob > _NO_SPEECH_THRESHOLD or seg.avg_logprob < _AVG_LOGPROB_THRESHOLD:
            dropped += 1
            continue
        for w in seg.words or []:
            words.append(WordTiming(
                start=float(w.start), end=float(w.end),
                word=w.word, probability=float(w.probability),
            ))
    logger.info("[Lyrics] transcribed %d words (dropped %d low-confidence segments)", len(words), dropped)
    return words


def transcribe_lyrics(audio_path: str, model_name: Optional[str] = None) -> List[LyricLine]:
    """识别并切行。返回空列表表示没有可用歌词（例如纯伴奏）。"""
    words = transcribe_words(audio_path, model_name=model_name)
    lines = group_words_into_lines(words)
    logger.info("[Lyrics] %d lines recognized", len(lines))
    return lines

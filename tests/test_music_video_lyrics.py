"""core.audio.lyrics 单元测试（v7.1 音乐视频）。

覆盖：词级时间戳切行规则、SRT 输出、模型名环境变量、模型缺失/加载失败降级、
转写结果的幻觉过滤。转写器使用替身模型，不下载 Whisper 权重、不触网。

用法:
    .venv/bin/python -m pytest tests/test_music_video_lyrics.py -q
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import srt  # noqa: E402

from core.audio import lyrics as lyr  # noqa: E402
from core.audio.lyrics import (  # noqa: E402
    LyricLine,
    LyricsUnavailableError,
    WordTiming,
    group_words_into_lines,
    lyric_lines_to_srt,
)


def _w(text: str, start: float, end: float, prob: float = 0.9) -> WordTiming:
    return WordTiming(start=start, end=end, word=text, probability=prob)


# ═══════════════════════════════════════════════════
# 切行规则
# ═══════════════════════════════════════════════════

class TestGroupWordsIntoLines:
    def test_empty_input_gives_no_lines(self):
        assert group_words_into_lines([]) == []

    def test_blank_tokens_are_ignored(self):
        words = [_w("  ", 0.0, 0.2), _w(" hi", 0.3, 0.9)]
        lines = group_words_into_lines(words)
        assert [line.text for line in lines] == ["hi"]

    def test_long_pause_starts_new_line(self):
        words = [
            _w(" hello", 0.0, 0.4), _w(" world", 0.5, 0.9),
            _w(" again", 3.0, 3.6), _w(" line", 3.7, 4.2),
        ]
        lines = group_words_into_lines(words)
        assert [line.text for line in lines] == ["hello world", "again line"]
        assert (lines[0].start, lines[0].end) == (0.0, 0.9)
        assert (lines[1].start, lines[1].end) == (3.0, 4.2)

    def test_max_words_splits_line(self):
        # 12 个词，词间隔 0.1 s，每词 0.5 s：前 10 词一行，后 2 词一行（时长 ≥ 0.6 s 不会被并回）
        words = [_w(f" w{i}", i * 0.6, i * 0.6 + 0.5) for i in range(12)]
        lines = group_words_into_lines(words)
        assert len(lines) == 2
        assert all(len(line.text.split()) <= 10 for line in lines)
        assert lines[0].text.split()[-1] == "w9"
        assert lines[1].text == "w10 w11"

    def test_max_visible_chars_splits_line(self):
        # 每词 4 个汉字、共 10 词 = 40 字符；单行上限 36 字符 → 前 9 词一行
        words = [_w("一二三四", i * 0.9, i * 0.9 + 0.8) for i in range(10)]
        lines = group_words_into_lines(words)
        assert len(lines) == 2
        assert len(lines[0].text) == 36
        assert lines[1].text == "一二三四"

    def test_short_line_is_merged_into_previous(self):
        # 第二行只有 0.2 秒（短于 0.6 秒）→ 并入第一行
        words = [_w(" one", 0.0, 1.0), _w(" x", 2.0, 2.2)]
        lines = group_words_into_lines(words)
        assert len(lines) == 1
        assert lines[0].text == "one x"
        assert lines[0].end == pytest.approx(2.2)

    def test_lines_are_monotonic_and_not_overlapping(self):
        words = [_w(f" t{i}", i * 0.7, i * 0.7 + 0.6) for i in range(25)]
        lines = group_words_into_lines(words)
        for prev, nxt in zip(lines, lines[1:]):
            assert prev.start <= nxt.start
            assert prev.end <= nxt.start + 1e-9


# ═══════════════════════════════════════════════════
# SRT 输出
# ═══════════════════════════════════════════════════

class TestLyricLinesToSrt:
    def test_no_lines_gives_empty_string(self):
        assert lyric_lines_to_srt([]) == ""

    def test_srt_format_and_round_trip(self):
        text = lyric_lines_to_srt([
            LyricLine(start=1.0, end=3.5, text="第一句"),
            LyricLine(start=4.0, end=6.25, text="第二句"),
        ])
        assert text.startswith("1\n00:00:01,000 --> 00:00:03,500\n第一句")
        assert "2\n00:00:04,000 --> 00:00:06,250\n第二句" in text
        parsed = list(srt.parse(text))
        assert [p.content for p in parsed] == ["第一句", "第二句"]
        assert parsed[1].end.total_seconds() == pytest.approx(6.25)


# ═══════════════════════════════════════════════════
# 模型名与加载降级
# ═══════════════════════════════════════════════════

class TestModelNameAndLoading:
    def test_default_model_is_small(self, monkeypatch):
        monkeypatch.delenv("AGNES_LYRICS_MODEL", raising=False)
        assert lyr.get_lyrics_model_name() == "small"

    def test_env_overrides_model(self, monkeypatch):
        monkeypatch.setenv("AGNES_LYRICS_MODEL", " medium ")
        assert lyr.get_lyrics_model_name() == "medium"

    def test_missing_package_raises_unavailable(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import → ImportError
        lyr._MODEL_CACHE.clear()
        with pytest.raises(LyricsUnavailableError, match="not installed"):
            lyr._load_model("small")

    def test_model_load_failure_is_wrapped(self, monkeypatch):
        fake = types.ModuleType("faster_whisper")

        class _Boom:
            def __init__(self, *args, **kwargs):
                raise OSError("no network")

        fake.WhisperModel = _Boom
        monkeypatch.setitem(sys.modules, "faster_whisper", fake)
        lyr._MODEL_CACHE.clear()
        with pytest.raises(LyricsUnavailableError, match="cannot load whisper model"):
            lyr._load_model("small")

    def test_model_is_cached_per_name(self, monkeypatch):
        created = []
        fake = types.ModuleType("faster_whisper")

        class _Model:
            def __init__(self, name, device, compute_type):
                created.append((name, device, compute_type))

        fake.WhisperModel = _Model
        monkeypatch.setitem(sys.modules, "faster_whisper", fake)
        lyr._MODEL_CACHE.clear()
        first = lyr._load_model("small")
        second = lyr._load_model("small")
        assert first is second
        assert created == [("small", "cpu", "int8")]
        lyr._MODEL_CACHE.clear()


# ═══════════════════════════════════════════════════
# 转写：幻觉过滤与端到端（替身模型）
# ═══════════════════════════════════════════════════

class _Word:
    def __init__(self, start, end, word, probability=0.9):
        self.start, self.end, self.word, self.probability = start, end, word, probability


class _Segment:
    def __init__(self, words, no_speech_prob=0.1, avg_logprob=-0.3):
        self.words = words
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob


class _FakeModel:
    def __init__(self, segments):
        self._segments = segments
        self.calls = []

    def transcribe(self, audio_path, **kwargs):
        self.calls.append((audio_path, kwargs))
        return iter(self._segments), types.SimpleNamespace(language="zh")


class TestTranscribe:
    def test_hallucinated_segments_are_dropped(self, monkeypatch):
        good = _Segment([_Word(0.0, 0.5, " 你好"), _Word(0.5, 1.0, " 世界")])
        noise = _Segment([_Word(5.0, 5.5, " thanks for watching")], no_speech_prob=0.9)
        low = _Segment([_Word(9.0, 9.5, " ghost")], avg_logprob=-1.5)
        model = _FakeModel([good, noise, low])
        monkeypatch.setattr(lyr, "_load_model", lambda name: model)

        words = lyr.transcribe_words("/tmp/song.mp3")

        assert [w.word for w in words] == [" 你好", " 世界"]
        _, kwargs = model.calls[0]
        assert kwargs["word_timestamps"] is True
        assert kwargs["vad_filter"] is False
        assert kwargs["condition_on_previous_text"] is False

    def test_transcribe_lyrics_end_to_end_with_fake_model(self, monkeypatch):
        seg = _Segment([
            _Word(1.0, 1.4, " Hello"), _Word(1.4, 1.9, " there"),
            _Word(4.0, 4.5, " second"), _Word(4.5, 5.2, " line"),
        ])
        monkeypatch.setattr(lyr, "_load_model", lambda name: _FakeModel([seg]))
        lines = lyr.transcribe_lyrics("/tmp/song.mp3")
        assert [line.text for line in lines] == ["Hello there", "second line"]

    def test_instrumental_song_gives_no_lines(self, monkeypatch):
        monkeypatch.setattr(lyr, "_load_model", lambda name: _FakeModel([]))
        assert lyr.transcribe_lyrics("/tmp/song.mp3") == []

    def test_unavailable_model_propagates_as_unavailable(self, monkeypatch):
        def _raise(name):
            raise LyricsUnavailableError("faster-whisper is not installed")

        monkeypatch.setattr(lyr, "_load_model", _raise)
        with pytest.raises(LyricsUnavailableError):
            lyr.transcribe_lyrics("/tmp/song.mp3")

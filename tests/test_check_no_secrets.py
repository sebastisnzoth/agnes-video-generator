"""
tests/test_check_no_secrets.py — 明文密钥门禁的自检

门禁脚本自己也得被测：一个「永远返回 0」的扫描器比没有扫描器更危险——
它会给出虚假的安全感。这里只覆盖三种情况：真 Key 必须被抓到、
占位符不能被误报、退出码语义正确。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from scripts import check_no_secrets as cns


@pytest.fixture
def sample_file(tmp_path):
    """返回一个可写入临时文件的工厂。"""

    def _write(name: str, content: str) -> str:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    return _write


def test_detects_real_keys(sample_file):
    """三种真实前缀都必须命中。"""
    path = sample_file(
        ".env.example",
        "AGNES_API_KEY=sk-HwdUtr6k7ZFNqAPIgnoGWenJS7b8zjVLxOg8NlFJwYOsJeCD\n"
        "GEMINI_API_KEY=AQ.Ab8RN6IE9TEHye4ON7UNVpBdWUUxn2eXj1yxV5wRkoM3u0yO0g\n",
    )
    hits = cns.scan([path])
    names = {h[2] for h in hits}
    assert len(hits) == 2, f"两把 Key 都应被检出：{hits}"
    assert names == {"Agnes/OpenAI 风格 Key", "Google Gemini Key"}


def test_placeholders_not_flagged(sample_file):
    """占位符 / 掩码值不应造成误报，否则 CI 会天天红。"""
    path = sample_file(
        ".env.example",
        "AGNES_API_KEY=your-agnes-api-key\n"
        "GEMINI_API_KEY=xxx\n"
        "OPENROUTER_API_KEY=<your-key>\n"
        "NOTE=sk-****\n",
    )
    assert cns.scan([path]) == []


def test_line_numbers_are_reported(sample_file):
    """报出的行号要能直接定位，否则排障成本太高。"""
    path = sample_file(".env.example", "# comment\nAGNES_API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345\n")
    hits = cns.scan([path])
    assert len(hits) == 1
    assert hits[0][1] == 2, "应指向第 2 行"


def test_main_exit_codes(sample_file, monkeypatch, capsys):
    """干净 → 0；命中 → 1。"""
    clean = sample_file("clean.env", "AGNES_API_KEY=your-agnes-api-key\n")
    monkeypatch.setattr(sys, "argv", ["check_no_secrets.py", clean])
    assert cns.main() == 0
    capsys.readouterr()

    dirty = sample_file("dirty.env", "AGNES_API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345\n")
    monkeypatch.setattr(sys, "argv", ["check_no_secrets.py", dirty])
    assert cns.main() == 1
    out = capsys.readouterr().out
    assert "轮换" in out, "命中时应提示轮换而非仅报告"

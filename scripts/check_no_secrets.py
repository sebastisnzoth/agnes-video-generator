#!/usr/bin/env python3
"""
check_no_secrets.py — 仓库内明文密钥扫描（CI 门禁，仅标准库）

存在的直接原因
--------------
``.env.example`` 的注释里明写着「示例文件，绝不要放入真实 Key！」，但某次提交
仍把三把**真实** Key（Agnes / Gemini / OpenRouter）直接填了进去并推送到公开仓库。
模板文件是会被 ``git add -A`` 提交的，一旦写入真实凭据就等于公开泄漏。

这里做的事很朴素：扫描给定路径，命中「像真 Key」的字符串就判红。
**不试图做通用密钥检测**——只覆盖本项目会用到的三种前缀，宁可漏报也不误报到
让 CI 天天红。

用法::

    python scripts/check_no_secrets.py                 # 扫描默认路径
    python scripts/check_no_secrets.py path/a path/b   # 扫描指定路径

退出码：0 = 干净；1 = 发现疑似明文密钥。
"""

from __future__ import annotations

import os
import re
import sys

# (名称, 正则)
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Agnes/OpenAI 风格 Key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("Google Gemini Key", re.compile(r"AQ\.[A-Za-z0-9_\-]{20,}")),
]

# 这些是文档/示例里的占位符，不是真 Key
ALLOWLIST = re.compile(
    r"your[-_ ]?\w*api[-_ ]?key|xxx+|\*{3,}|<[^>]+>|\.\.\.|REDACTED|placeholder",
    re.IGNORECASE,
)

DEFAULT_PATHS = [
    ".env.example",
    "static",
    "frontend/src",
    "vercel.json",
    "render.yaml",
]

# 扫描时跳过的目录
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "htmlcov"}

# 只扫这些后缀，避免对着图片/字体做无意义的正则匹配
SCAN_SUFFIXES = {
    ".py", ".ts", ".js", ".vue", ".json", ".md", ".yml", ".yaml",
    ".example", ".env", ".txt", ".html", ".css", ".ini", ".cfg", ".toml",
}

# 没有后缀但必须扫的文件（如 .env.example 的 basename）
ALWAYS_SCAN_BASENAMES = {".env.example", ".env", "Dockerfile"}


def iter_files(paths: list[str]):
    for p in paths:
        if os.path.isfile(p):
            yield p
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if name in ALWAYS_SCAN_BASENAMES or os.path.splitext(name)[1] in SCAN_SUFFIXES:
                    yield os.path.join(root, name)


def scan(paths: list[str]) -> list[tuple[str, int, str, str]]:
    """返回 ``(文件, 行号, 规则名, 片段)`` 列表。"""
    hits: list[tuple[str, int, str, str]] = []
    for path in iter_files(paths):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    if ALLOWLIST.search(line):
                        continue
                    for name, pat in PATTERNS:
                        m = pat.search(line)
                        if m:
                            token = m.group(0)
                            hits.append((path, lineno, name, token[:8] + "…"))
        except OSError:
            continue
    return hits


def main() -> int:
    paths = sys.argv[1:] or [p for p in DEFAULT_PATHS if os.path.exists(p)]
    if not paths:
        print("[check_no_secrets] 没有可扫描路径，跳过")
        return 0

    hits = scan(paths)
    if hits:
        print(f"[check_no_secrets] ❌ 在 {len(set(h[0] for h in hits))} 个文件中发现疑似明文密钥：")
        for path, lineno, name, snippet in hits:
            print(f"  {path}:{lineno}  [{name}]  {snippet}")
        print("\n如果是真实凭据：立即在提供方后台吊销/轮换，并把它从 git 历史里清除。")
        print("删除文件里的值**不等于**密钥失效——已经推送到公开仓库的凭据必须轮换。")
        return 1

    print(f"[check_no_secrets] ✅ 未发现明文密钥（已扫描 {len(paths)} 个路径）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

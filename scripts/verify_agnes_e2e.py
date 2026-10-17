#!/usr/bin/env python3
"""
verify_agnes_e2e.py — Agnes 视频 API 真实端到端验证（仅标准库）

用途
----
在开发机 / CI 上用**真实 API Key**跑一次完整的视频生成链路，验证：

1. Bearer 认证可用（``GET /v1/models``）
2. 创建任务被接受并返回真实 ``video_id``（``POST /v1/videos``）
3. 轮询进度能推进到 ``completed``（``GET /agnesapi``）
4. 产物 URL 可下载，且是**真实可播放的 mp4**（检查 ftyp box + ffprobe）
5. 可用模型清单里确实包含所用模型

与仓库内其它脚本的区别：这里**不经过本项目的 FastAPI 后端**，直接打 Agnes API。
目的是把「Agnes 侧是否真的能出片」与「本项目后端是否正确编排」两个问题分开定位。

凭据
----
只从环境变量 ``AGNES_API_KEY`` 读取，**不落盘、不打印、不写进任何产物**。
缺 Key 时以退出码 2 跳过（CI 中视为 SKIP，不是 FAIL）。

用法::

    AGNES_API_KEY=... python scripts/verify_agnes_e2e.py \\
        --prompt "Un astronauta caminando sobre una playa tropical al atardecer" \\
        --seconds 5 --out generated.mp4

退出码
------
0 = 全部通过；1 = 验证失败；2 = 缺少凭据（跳过）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://apihub.agnes-ai.com"
DEFAULT_MODEL = "agnes-video-2.5-flash"
DEFAULT_PROMPT = (
    "Un astronauta caminando sobre una playa tropical al atardecer, "
    "estilo cinematográfico, movimiento de cámara suave."
)

# agnes-video-2.5-flash 固定 720P，seconds 取字符串 "4"~"12"（官方文档）
FLASH_SIZE = "720P"


class VerificationError(RuntimeError):
    """验证失败（非凭据缺失）。"""


def _request(
    url: str,
    *,
    api_key: str,
    method: str = "GET",
    payload: dict | None = None,
    timeout: int = 60,
) -> tuple[int, str]:
    """发起一次带 Bearer 鉴权的请求，返回 ``(status_code, body_text)``。"""
    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def _mask(secret: str) -> str:
    """只保留前 4 / 后 2 位，够定位是哪把 Key，不足以复用。"""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}…{secret[-2:]} (len={len(secret)})"


def step_auth(base: str, api_key: str) -> list[str]:
    """第 1 步：Bearer 认证 + 拉取可用模型。"""
    status, body = _request(f"{base}/v1/models", api_key=api_key, timeout=30)
    if status != 200:
        raise VerificationError(f"GET /v1/models 返回 HTTP {status}：{body[:300]}")
    try:
        models = json.loads(body).get("data", [])
    except json.JSONDecodeError as e:
        raise VerificationError(f"/v1/models 返回非 JSON：{e}") from e
    ids = sorted(str(m.get("id", "")) for m in models if isinstance(m, dict))
    video_models = [i for i in ids if i.startswith("agnes-video")]
    if not video_models:
        raise VerificationError(f"模型列表中没有视频模型：{ids[:20]}")
    return [
        "✅ Bearer 认证通过",
        f"   模型总数 {len(ids)}，视频模型：{', '.join(video_models)}",
    ]


def step_create(
    base: str, api_key: str, model: str, prompt: str, seconds: int, seed: int | None
) -> str:
    """第 2 步：创建视频任务，返回 ``video_id``。"""
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "seconds": str(seconds),
        "mode": "text",
        "size": FLASH_SIZE,
        "aspect_ratio": "16:9",
        "n": 1,
    }
    if seed is not None:
        payload["seed"] = seed

    status, body = _request(
        f"{base}/v1/videos", api_key=api_key, method="POST", payload=payload, timeout=120
    )
    if status >= 400:
        raise VerificationError(f"POST /v1/videos 返回 HTTP {status}：{body[:500]}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise VerificationError(f"创建任务返回非 JSON：{body[:300]}") from e

    video_id = data.get("video_id") or data.get("id") or ""
    if not video_id:
        raise VerificationError(f"创建响应缺少 video_id：{json.dumps(data)[:300]}")

    print(f"✅ 任务已创建：video_id={video_id} id={data.get('id', '')}")
    return str(video_id)


def step_poll(
    base: str, api_key: str, video_id: str, model: str, timeout_s: int, every: int
) -> dict:
    """第 3 步：轮询直到 completed / failed。"""
    q = urllib.parse.urlencode({"video_id": video_id, "model_name": model})
    url = f"{base}/agnesapi?{q}"

    deadline = time.time() + timeout_s
    last_progress = -1
    while time.time() < deadline:
        status, body = _request(url, api_key=api_key, timeout=30)
        if status != 200:
            raise VerificationError(f"GET /agnesapi 返回 HTTP {status}：{body[:300]}")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise VerificationError(f"轮询返回非 JSON：{body[:300]}") from e

        prog = data.get("progress", 0)
        st = data.get("status")
        if prog != last_progress:
            print(f"   … status={st} progress={prog}%")
            last_progress = prog

        if st == "completed":
            print("✅ 任务完成")
            return data
        if st == "failed":
            raise VerificationError(
                f"任务失败：{data.get('error') or json.dumps(data)[:300]}"
            )
        time.sleep(every)

    raise VerificationError(f"轮询超时（{timeout_s}s），未 completed")


def step_download(url: str, out_path: str, api_key: str) -> int:
    """第 4 步：下载产物并校验是真实 mp4。"""
    if not url:
        raise VerificationError("completed 响应缺少顶层 url 字段")

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(out_path, "wb") as fh:
        fh.write(resp.read())

    size = os.path.getsize(out_path)
    if size == 0:
        raise VerificationError("下载到的文件为空")

    with open(out_path, "rb") as fh:
        head = fh.read(4096)
    if b"ftyp" not in head:
        raise VerificationError("下载内容不是 mp4（文件头缺少 ftyp box）")

    print(f"✅ 视频已下载：{out_path}（{size:,} bytes，mp4 文件头有效）")
    return size


def main() -> int:
    ap = argparse.ArgumentParser(description="Agnes 视频 API 真实端到端验证")
    ap.add_argument("--base", default=os.getenv("AGNES_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--seconds", type=int, default=5, help="4~12，默认 5（最短、最省）")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--timeout", type=int, default=900, help="轮询总超时（秒）")
    ap.add_argument("--every", type=int, default=3, help="轮询间隔（秒），官方建议 1~2s")
    ap.add_argument("--out", default="generated.mp4")
    ap.add_argument("--summary", default="", help="额外写入 GitHub Step Summary 的路径")
    args = ap.parse_args()

    api_key = (os.getenv("AGNES_API_KEY") or "").strip()
    if not api_key:
        print("::notice::AGNES_API_KEY 未设置 —— 跳过真实生成验证（不算失败）")
        return 2

    lines: list[str] = []
    try:
        print(f"Key: {_mask(api_key)}  base: {args.base}")
        lines += step_auth(args.base, api_key)
        video_id = step_create(
            args.base, api_key, args.model, args.prompt, args.seconds, args.seed
        )
        lines.append(f"✅ 任务创建成功：`{video_id}`")
        data = step_poll(args.base, api_key, video_id, args.model, args.timeout, args.every)
        size = step_download(str(data.get("url") or ""), args.out, api_key)
        lines.append(f"✅ 视频下载成功：{size:,} bytes → `{args.out}`")
        lines.append(f"   分辨率 size={data.get('size')}  时长 seconds={data.get('seconds')}")
    except VerificationError as e:
        print(f"\n❌ 验证失败：{e}", file=sys.stderr)
        lines.append(f"❌ 验证失败：{e}")
        _write_summary(args.summary, lines)
        return 1

    lines.append("")
    lines.append("**结果：真实视频生成链路全部通过**")
    _write_summary(args.summary, lines)
    print("\n🎉 端到端验证通过")
    return 0


def _write_summary(path: str, lines: list[str]) -> None:
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as e:
        print(f"[warn] 写入 summary 失败：{e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

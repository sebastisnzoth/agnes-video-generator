"""
server.py 应用层单测 — tests/test_server_app.py

覆盖 server.py 中独立于 web/routes 的应用层逻辑（test_routes.py 不经过 server.py，
本文件补齐）：
- lifespan 生命周期：正常启动 / 音色加载超时（转后台）/ 音色加载失败（fallback）
  / 僵尸任务清理（启用 + 异常不阻断）
- 静态资源：/static 挂载、/favicon.ico、/icon.png（存在 200）、缺失 404
- _serve_static_file：存在 → FileResponse，缺失 → HTTPException(404)
- __main__ 入口块：HOST/PORT 环境变量、优雅退出、二次 Ctrl+C 强制退出

写路径隔离：init_runtime_state / load_voice_catalog / sweep_stale_tasks 全部打桩，
测试不触碰真实工作区、不发网络请求。

用法:
    .venv/bin/python -m pytest tests/test_server_app.py -v
"""

import asyncio
import os
import runpy
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

import server as server_mod


async def _async_noop(*args, **kwargs):
    """lifespan 中音色目录加载的桩实现（立即返回空目录）。"""
    return {}


@pytest.fixture(autouse=True)
def _isolate_lifespan_side_effects(monkeypatch):
    """所有用例默认打桩 lifespan 副作用，避免触碰真实工作区 / 网络。"""
    monkeypatch.setattr(server_mod, "init_runtime_state", lambda: None)
    monkeypatch.setattr(server_mod, "load_voice_catalog", _async_noop)
    monkeypatch.delenv("AGNES_SWEEP_AGE_DAYS", raising=False)


# ═══════════════════════════════════════════════
# 1. lifespan 生命周期
# ═══════════════════════════════════════════════

class TestLifespan:
    def test_healthy_startup(self, monkeypatch):
        """正常启动：运行时初始化 + 音色目录加载完成，无 sweep 配置。"""
        init_calls = []
        monkeypatch.setattr(server_mod, "init_runtime_state", lambda: init_calls.append(1))
        with TestClient(server_mod.app) as client:
            assert init_calls == [1], "lifespan 应调用 init_runtime_state"
            assert client.get("/favicon.ico").status_code == 200

    def test_voice_timeout_falls_back_to_background(self, monkeypatch):
        """音色加载超过 3s → TimeoutError → 转入后台任务，服务仍可用。

        只替换 server 模块命名空间内的 asyncio（lifespan 闭包解析全局名），
        不污染全局 asyncio.wait_for，避免影响 TestClient 内部实现。
        """
        class _FakeAsyncio:
            TimeoutError = asyncio.TimeoutError  # lifespan 的 `except asyncio.TimeoutError`

            @staticmethod
            def wait_for(coro, timeout):
                coro.close()  # 避免 coroutine never awaited 警告
                raise asyncio.TimeoutError()

            create_task = staticmethod(asyncio.create_task)

        monkeypatch.setattr(server_mod, "asyncio", _FakeAsyncio)
        with TestClient(server_mod.app) as client:
            # 发请求驱动事件循环，让后台加载任务执行完毕
            assert client.get("/icon.png").status_code == 200

    def test_voice_background_load_failure_logs_warning(self, monkeypatch):
        """音色加载超时转入后台后，后台加载仍失败 → 仅告警不崩溃。"""
        class _FakeAsyncio:
            TimeoutError = asyncio.TimeoutError

            @staticmethod
            def wait_for(coro, timeout):
                coro.close()
                raise asyncio.TimeoutError()

            create_task = staticmethod(asyncio.create_task)

        async def _boom(*args, **kwargs):
            raise RuntimeError("background load failed")

        monkeypatch.setattr(server_mod, "asyncio", _FakeAsyncio)
        monkeypatch.setattr(server_mod, "load_voice_catalog", _boom)
        with TestClient(server_mod.app) as client:
            assert client.get("/favicon.ico").status_code == 200

    def test_voice_load_failure_uses_fallback(self, monkeypatch):
        """音色目录加载抛异常 → 服务仍正常对外（fallback 目录）。"""
        async def _boom(*args, **kwargs):
            raise RuntimeError("edge_tts unreachable")

        monkeypatch.setattr(server_mod, "load_voice_catalog", _boom)
        with TestClient(server_mod.app) as client:
            assert client.get("/favicon.ico").status_code == 200

    def test_sweep_enabled_on_startup(self, monkeypatch):
        """设置 AGNES_SWEEP_AGE_DAYS → 启动时执行僵尸任务清理。"""
        calls = {}

        def _fake_sweep(age_days, protect_statuses=None):
            calls["age_days"] = age_days
            return {"swept": 1, "protected": []}

        monkeypatch.setenv("AGNES_SWEEP_AGE_DAYS", "7")
        monkeypatch.setattr("core.artifacts.sweep_stale_tasks", _fake_sweep)
        with TestClient(server_mod.app):
            pass
        assert calls["age_days"] == 7, "应使用 AGNES_SWEEP_AGE_DAYS 作为清理阈值"

    def test_sweep_failure_does_not_block_startup(self, monkeypatch):
        """清理异常 → 仅告警，不阻断服务启动。"""
        def _boom(age_days, protect_statuses=None):
            raise RuntimeError("sweep failed")

        monkeypatch.setenv("AGNES_SWEEP_AGE_DAYS", "3")
        monkeypatch.setattr("core.artifacts.sweep_stale_tasks", _boom)
        with TestClient(server_mod.app) as client:
            assert client.get("/favicon.ico").status_code == 200


# ═══════════════════════════════════════════════
# 1b. lifespan 启用条件（Vercel 适配回归防护）
# ═══════════════════════════════════════════════

class TestLifespanEnablement:
    """lifespan 只能在 Vercel serverless 下关闭，不得无条件关掉。

    回归背景：为规避 Vercel ASGI adapter 的 lifespan 递归，曾把 ``server.py`` 的
    ``FastAPI(lifespan=...)`` 改成**无条件** ``None``。结果本地 / Docker / start.sh
    启动时 ``init_runtime_state()`` 不再执行 → 工作目录与 uploads 不创建、音色目录
    不预加载、僵尸任务清理与 ffmpeg 检测全部静默失效。
    """

    def test_lifespan_runs_when_not_on_vercel(self, monkeypatch):
        """非 Vercel 环境：lifespan 必须执行 init_runtime_state。"""
        calls = []
        monkeypatch.setattr(server_mod, "init_runtime_state", lambda: calls.append(1))
        assert server_mod._RUNNING_ON_VERCEL is False, "测试环境不应设置 VERCEL"
        with TestClient(server_mod.app):
            pass
        assert calls == [1], "本地/Docker 启动必须执行 lifespan 初始化"

    def test_lifespan_disabled_only_on_vercel(self, monkeypatch):
        """仅 VERCEL 环境变量存在时关闭 lifespan。"""
        import importlib

        calls = []
        monkeypatch.setenv("VERCEL", "1")
        try:
            reloaded = importlib.reload(server_mod)
            assert reloaded._RUNNING_ON_VERCEL is True, "VERCEL 存在时应判定为 serverless"
            monkeypatch.setattr(reloaded, "init_runtime_state", lambda: calls.append(1))
            with TestClient(reloaded.app):
                pass
            assert calls == [], "Vercel 下 lifespan 应关闭（避免 ASGI adapter 递归）"
        finally:
            # 复位：模块级 app 一旦被 reload 改写会影响后续用例
            monkeypatch.delenv("VERCEL", raising=False)
            importlib.reload(server_mod)


# ═══════════════════════════════════════════════
# 2. 静态资源
# ═══════════════════════════════════════════════

class TestStaticAssets:
    def test_static_mount_serves_index(self):
        with TestClient(server_mod.app) as client:
            resp = client.get("/static/index.html")
            assert resp.status_code == 200
            assert "text/html" in resp.headers["content-type"]

    def test_favicon_served(self):
        with TestClient(server_mod.app) as client:
            resp = client.get("/favicon.ico")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("image/x-icon")

    def test_icon_served(self):
        with TestClient(server_mod.app) as client:
            resp = client.get("/icon.png")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("image/png")

    def test_serve_static_file_returns_fileresponse(self):
        resp = server_mod._serve_static_file("favicon.ico", "image/x-icon")
        assert isinstance(resp, FileResponse)

    def test_serve_static_file_missing_raises_404(self):
        with pytest.raises(HTTPException) as exc:
            server_mod._serve_static_file("missing.ico", "image/x-icon")
        assert exc.value.status_code == 404


# ═══════════════════════════════════════════════
# 3. __main__ 入口块（python server.py）
# ═══════════════════════════════════════════════

class TestMainEntry:
    @pytest.fixture(autouse=True)
    def _clean_shutdown_event(self):
        """shutdown_event 为模块级共享 asyncio.Event，测试前后复位。"""
        server_mod.shutdown_event.clear()
        yield
        server_mod.shutdown_event.clear()

    def _run_as_main(self, monkeypatch, run_impl, original_handle_exit=None):
        """以 __main__ 重新执行 server.py，patch 掉 uvicorn 启动。

        original_handle_exit: 模拟 uvicorn.Server 原有的可调用 handle_exit，
        用于覆盖 `if callable(original_handle_exit)` 分支。
        """
        import uvicorn

        captured = {}

        class FakeServer:
            def __init__(self, config):
                captured["config"] = config
                # 实例属性保存函数对象，避免类属性函数经实例读取时被绑定为方法
                self.handle_exit = original_handle_exit

            def run(self):
                run_impl(self)

        monkeypatch.setattr(
            uvicorn, "Config",
            lambda app, host, port, log_level: (app, host, port, log_level),
        )
        monkeypatch.setattr(uvicorn, "Server", FakeServer)
        runpy.run_path(str(Path(server_mod.__file__).resolve()), run_name="__main__")
        return captured

    def test_main_block_respects_env_and_graceful_exit(self, monkeypatch):
        """HOST/PORT 环境变量生效；首次退出信号走优雅退出（不强制 exit）。"""
        monkeypatch.setenv("HOST", "127.0.0.1")
        monkeypatch.setenv("PORT", "9999")
        exited = []
        original_calls = []
        monkeypatch.setattr(os, "_exit", lambda code: exited.append(code))

        def _original(sig, frame):
            original_calls.append((sig, frame))

        def _run(server):
            server.handle_exit(None, None)  # 第一次信号 → 优雅退出

        captured = self._run_as_main(monkeypatch, _run, original_handle_exit=_original)
        app, host, port, log_level = captured["config"]
        assert app is not None
        assert host == "127.0.0.1"
        assert port == 9999
        assert log_level == "info"
        assert exited == [], "优雅退出不应调用 os._exit"
        assert original_calls == [(None, None)], "应透传调用原始退出处理器"
        assert server_mod.shutdown_event.is_set(), "优雅退出应置位 shutdown_event"

    def test_main_block_second_signal_force_exits(self, monkeypatch):
        """shutdown_event 已置位后再次退出信号 → os._exit(1) 强制退出。

        未显式设置 PORT，验证默认值 8765 分支。
        """
        monkeypatch.setenv("HOST", "0.0.0.0")
        exited = []
        monkeypatch.setattr(os, "_exit", lambda code: exited.append(code))

        def _run(server):
            server.handle_exit(None, None)  # 第一次：优雅退出（置位 shutdown_event）
            server.handle_exit(None, None)  # 第二次：强制退出

        captured = self._run_as_main(monkeypatch, _run)
        app, host, port, log_level = captured["config"]
        assert port == 8765, "未设置 PORT 时应使用默认 8765"
        assert exited == [1], "第二次信号应强制 os._exit(1)"

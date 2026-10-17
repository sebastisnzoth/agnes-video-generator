"""
tests/conftest.py — 全局测试隔离夹具

背景（本文件存在的原因）
----------------------
``core/config.py`` 在**导入时**就把仓库根目录的 ``.env`` 注入 ``os.environ``，
并且提供 ``_dotenv_value()`` 在**调用时**直接回读 ``.env`` 文件原文。

结果是：任何按 ``.env.example`` 文档执行过 ``cp .env.example .env`` 并填入真实
Key 的开发者，本地跑单测都会失败——``monkeypatch.delenv("AGNES_API_KEY")`` 只能
删掉环境变量，删不掉 ``_dotenv_value()`` 这条直接从文件读取的旁路，于是
``get_api_key()`` 仍返回真实 Key，断言失败。

这不只是「红灯难看」：测试会真实读到开发者本机密钥，属于测试对宿主环境的
隐式依赖。这里统一把 ``.env`` 旁路指向一个不存在的路径，让单测在任何机器上
行为一致（CI 本来就没有 .env，因此不改变 CI 结果）。

说明：需要验证 .env 解析行为的用例（如
``tests/test_processor_screenwriter.py``）在用例内自行
``monkeypatch.setattr(config_mod, "_dotenv_value", ...)``，优先级高于本夹具，
不受影响。
"""

import os
import tempfile

import pytest

from core import config as config_mod

# 指向一个确定不存在的路径：``dotenv_values()`` 对缺失文件返回空 dict，
# ``_dotenv_value()`` 随即返回 ""，等价于「没有 .env」。
_MISSING_DOTENV = os.path.join(
    tempfile.gettempdir(), "agnes-video-generator-tests-absent.env"
)


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch):
    """切断单测对开发者本地 .env 的读取（autouse，全量生效）。"""
    monkeypatch.setattr(config_mod, "_dotenv_path", _MISSING_DOTENV)

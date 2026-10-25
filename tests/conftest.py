"""测试公共夹具。

lifespan 会启动调度器（注册每天 07:00 的定时任务），测试若放它真跑，用例就会挂上真调度器。
所以这里给一个**显式**夹具，需要真实调度器的用例不请求它。
启动补拉已于 2026-10-15 取消，重启不再抓取 / 打分 / 采集，这里也就无需再挡补拉。
"""

from __future__ import annotations

import pytest

from app import scheduler


class NoopScheduler:
    """`scheduler.start()` 的替身：只提供 main 会调用的 `shutdown()`。"""

    def shutdown(self, *args, **kwargs) -> None:
        pass


@pytest.fixture()
def no_scheduler(monkeypatch):
    """把调度器换成空壳（`web` 这类走真实 lifespan 的夹具用它）。"""
    monkeypatch.setattr(scheduler, "start", lambda config: NoopScheduler())


@pytest.fixture(autouse=True)
def _no_llm_key(monkeypatch):
    """测试一律不发真实 LLM 请求：把 Key **置空**（铜币描述/备注翻译随即回退内置词表）。

    注意必须是「置空」而不是 `delenv`：`config.load()` 会 `load_dotenv(".env", override=False)`——
    环境变量**不存在**时 .env 里的真实 Key 会被读回来，测试就真的打到线上模型了；
    变量存在但为空串时 dotenv 不覆盖，`config.llm.api_key` 才是空。

    要测大模型路径的用例自己 `FakeClient` 注入（见 `tests/test_coins_translate.py`）。
    """
    monkeypatch.setenv("DESKHUB_LLM_API_KEY", "")

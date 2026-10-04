"""测试公共夹具。

lifespan 现在会启动调度器并 `create_task` 首轮抓取（M6-1）。测试若放它真跑，
每个走 lifespan 的用例都会去抓 6 个源 —— 与「测试不发真实网络请求」冲突
（plan §13.1 的前提）。所以这里给一个**显式**夹具，需要真实调度器的用例不请求它。
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
    """把调度器与启动补拉换成空壳（`web` 这类走真实 lifespan 的夹具用它）。"""
    monkeypatch.setattr(scheduler, "start", lambda config: NoopScheduler())

    async def _noop(config) -> None:
        return None

    monkeypatch.setattr(scheduler, "startup_fetch", _noop)

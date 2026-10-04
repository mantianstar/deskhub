"""调度器：每日任务的触发点与参数、错过触发点仍补跑、启动补拉不向上抛（plan §11.1 / §11.2）。

运行方式：`.venv/bin/python -m pytest tests/test_scheduler.py -q`
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import scheduler
from app.config import (
    AppConfig,
    Config,
    DigestConfig,
    FetchConfig,
    LLMConfig,
    ModuleConfig,
    ScoringConfig,
)


def _config(tmp_path: Path) -> Config:
    """配置对象直接构造，不读真实 config.yaml（测试与运行环境解耦）。"""
    return Config(
        app=AppConfig(
            host="127.0.0.1",
            port=8765,
            timezone="Asia/Shanghai",
            db_path=tmp_path / "test.db",
            log_path=tmp_path / "test.log",
        ),
        digest=DigestConfig(limit=8, lookback_hours=48),
        fetch=FetchConfig(
            timeout_seconds=10,
            user_agent="deskhub-test/1.0",
            max_items_per_source=30,
            max_concurrency=1,
        ),
        scoring=ScoringConfig(
            concurrency=3,
            max_summary_chars=1500,
            max_attempts=2,
            prompt_version="v1",
            temperature=0.2,
        ),
        llm=LLMConfig(
            base_url="https://api.example.com/v1",
            model="test-model",
            api_key_env="DESKHUB_TEST_KEY",
            api_key="",
        ),
        modules={
            "agent": ModuleConfig(key="agent", label="Agent 开发", focus="Agent 方向"),
            "bigdata": ModuleConfig(key="bigdata", label="大数据", focus="大数据方向"),
        },
        sources=(),
    )


@pytest.mark.asyncio
async def test_daily_job_fires_at_8am_local_with_plan_settings(tmp_path):
    """每日任务：08:00 业务时区，coalesce / misfire_grace_time / max_instances 按 plan §11.2。"""
    active = scheduler.start(_config(tmp_path))
    try:
        job = active.get_job(scheduler.DAILY_JOB_ID)
        assert job is not None
        assert job.coalesce is True
        assert job.misfire_grace_time == scheduler.MISFIRE_GRACE_SECONDS == 3600
        assert job.max_instances == 1

        fields = {field.name: str(field) for field in job.trigger.fields}
        assert fields["hour"] == "8"
        assert fields["minute"] == "0"
        assert str(job.trigger.timezone) == "Asia/Shanghai"
    finally:
        active.shutdown(wait=False)


@pytest.mark.asyncio
async def test_missed_run_within_grace_still_fires(tmp_path):
    """08:00 那一刻本机没醒：只要还在 grace 内，进程起来后照样补跑，而不是被丢掉。"""
    active = scheduler.start(_config(tmp_path))
    fired = asyncio.Event()

    async def probe() -> None:
        fired.set()

    try:
        active.add_job(
            probe,
            trigger="date",
            run_date=datetime.now(active.timezone) - timedelta(minutes=30),
            id="missed_probe",
            misfire_grace_time=scheduler.MISFIRE_GRACE_SECONDS,
        )
        await asyncio.wait_for(fired.wait(), timeout=5)
        assert fired.is_set()
    finally:
        active.shutdown(wait=False)


@pytest.mark.asyncio
async def test_run_missed_beyond_grace_is_dropped(tmp_path):
    """对照组：错过超过 grace 就不再补跑 —— 证明补跑是 grace 在起作用，不是「必跑」。"""
    active = scheduler.start(_config(tmp_path))
    fired = asyncio.Event()

    async def probe() -> None:
        fired.set()

    try:
        active.add_job(
            probe,
            trigger="date",
            run_date=datetime.now(active.timezone) - timedelta(seconds=90),
            id="late_probe",
            misfire_grace_time=1,
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(fired.wait(), timeout=1)
    finally:
        active.shutdown(wait=False)


@pytest.mark.asyncio
async def test_startup_fetch_swallows_errors(tmp_path, monkeypatch):
    """启动补拉跑在 `create_task` 里，异常必须就地兜住，服务不能因一轮抓取退出。"""

    async def failing_run(config) -> None:
        raise RuntimeError("抓取炸了")

    monkeypatch.setattr(scheduler.pipeline, "run", failing_run)

    assert await scheduler.startup_fetch(_config(tmp_path)) is None

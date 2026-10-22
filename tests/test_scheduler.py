"""调度器：每日任务的触发点与参数、错过触发点仍补跑、启动补拉不向上抛（plan §11.1 / §11.2）。

运行方式：`.venv/bin/python -m pytest tests/test_scheduler.py -q`
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import db, scheduler, switches
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
async def test_daily_pipeline_respects_switches(tmp_path, monkeypatch):
    """每天 08:00 那一轮按开关走：关掉抓取就只打分，两个都关就什么都不做（plan §19）。"""
    cfg = _config(tmp_path)
    db.init_db(cfg.app.db_path)

    called: list[str] = []

    async def fake_fetch(config) -> str:
        called.append("fetch")
        return "抓取完成"

    async def fake_score(config) -> str:
        called.append("score")
        return "打分完成"

    monkeypatch.setattr(scheduler.tasks, "run_knowledge_fetch", fake_fetch)
    monkeypatch.setattr(scheduler.tasks, "run_knowledge_score", fake_score)

    await scheduler.daily_pipeline(cfg)
    assert called == ["fetch", "score"]

    switches.toggle(switches.KNOWLEDGE_FETCH)  # 关掉抓取
    called.clear()
    await scheduler.daily_pipeline(cfg)
    assert called == ["score"]

    switches.toggle(switches.KNOWLEDGE_SCORE)  # 再关掉打分 → 整轮无事可做
    called.clear()
    await scheduler.daily_pipeline(cfg)
    assert called == []


@pytest.mark.asyncio
async def test_daily_pipeline_swallows_nothing(tmp_path, monkeypatch):
    """抓取失败要向上抛给 APScheduler（job 不会被摘掉），与改造前一致。"""

    async def failing_fetch(config) -> str:
        raise RuntimeError("抓取炸了")

    cfg = _config(tmp_path)
    db.init_db(cfg.app.db_path)
    monkeypatch.setattr(scheduler.tasks, "run_knowledge_fetch", failing_fetch)

    with pytest.raises(RuntimeError):
        await scheduler.daily_pipeline(cfg)


@pytest.mark.asyncio
async def test_daily_coins_swallows_errors(tmp_path, monkeypatch):
    """铜币定时任务也要就地兜异常：网关不可用是常态，
    不该把堆栈丢给 APScheduler，更不能影响知识日报（spec §14.9）。"""
    from dataclasses import replace

    async def failing_run(config, **kwargs) -> None:
        raise RuntimeError("网关调用失败：502")

    monkeypatch.setattr(scheduler.coins_pipeline, "run", failing_run)

    cfg = _config(tmp_path)
    # daily_coins 要先读功能开关（默认全开），所以库得建好
    db.init_db(cfg.app.db_path)
    enabled = replace(cfg, coins=replace(cfg.coins, enabled=True))
    assert await scheduler.daily_coins(enabled) is None
    # 未启用时直接返回，连任务日志都不打
    assert await scheduler.daily_coins(cfg) is None


@pytest.mark.asyncio
async def test_daily_coins_skips_when_all_switches_off(tmp_path):
    """采集、回采、查价都关掉时，铜币那一轮直接跳过，不碰网关（plan §19）。"""
    from dataclasses import replace

    cfg = _config(tmp_path)
    db.init_db(cfg.app.db_path)
    switches.toggle(switches.COINS_COLLECT)
    switches.toggle(switches.COINS_FINALIZE)
    switches.toggle(switches.COINS_PRICE)

    enabled = replace(cfg, coins=replace(cfg.coins, enabled=True))
    assert await scheduler.daily_coins(enabled) is None
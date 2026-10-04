"""APScheduler 任务定义（plan §11.2）。

两个入口，走法有意不同：

- `startup_fetch`：启动补拉，一次性，覆盖休眠/关机期间漏跑（spec §9）。由 lifespan 用
  `asyncio.create_task` 直接调起，**不经过 APScheduler** —— 首轮抓取可能几十秒，
  不能让首页等到超时（plan §11.1 第 6 步）。
- `daily_pipeline`：每天 `08:00`（`config.app.timezone`）出报，交给 APScheduler。

不依赖 FastAPI；`app/main.py` 只负责把两者接进 lifespan。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import pipeline
from app.config import Config
from app.pipeline import PipelineResult

logger = logging.getLogger(__name__)

DAILY_JOB_ID = "daily_pipeline"
DAILY_HOUR = 8
# 本机 08:05 才醒，这一轮仍要执行而不是被丢弃（plan §11.2）
MISFIRE_GRACE_SECONDS = 3600


async def startup_fetch(config: Config) -> PipelineResult | None:
    """启动补拉：跑一轮管道；失败只记日志，不让服务起不来。"""
    logger.info("启动补拉开始")
    try:
        result = await pipeline.run(config)
    except Exception:
        # 它是 `create_task` 起的后台任务，抛出去只会变成
        # 「Task exception was never retrieved」；一轮抓取失败不该带走服务
        logger.exception("启动补拉失败，等下一次定时任务重试")
        return None
    logger.info(
        "启动补拉结束：新增=%d 打分成功=%d token(total=%d)",
        result.items_new,
        result.score.items_scored,
        result.score.total_tokens,
    )
    return result


async def daily_pipeline(config: Config) -> PipelineResult:
    """每日出报：抓取 + 打分。异常交给 APScheduler 记录，job 不会被摘掉。"""
    logger.info("定时任务触发：%s", DAILY_JOB_ID)
    return await pipeline.run(config)


def start(config: Config) -> AsyncIOScheduler:
    """建调度器、注册每日任务并启动。

    时区取 `config.app.timezone`，所以 `08:00` 是本地时间；`coalesce=True` 让「错过多次」
    只补跑一次，`max_instances=1` 让定时任务不与上一轮重叠（plan §11.2）。
    """
    scheduler = AsyncIOScheduler(timezone=config.app.tz)
    scheduler.add_job(
        daily_pipeline,
        trigger="cron",
        hour=DAILY_HOUR,
        minute=0,
        args=[config],
        id=DAILY_JOB_ID,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        "调度器已启动：%s 每天 %02d:00（%s）", DAILY_JOB_ID, DAILY_HOUR, config.app.timezone
    )
    return scheduler

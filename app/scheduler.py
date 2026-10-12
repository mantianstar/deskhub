"""APScheduler 任务定义（plan §11.2、§18.9）。

四个入口，走法有意不同：

- `startup_fetch`：启动补拉，一次性，覆盖休眠/关机期间漏跑（spec §9）。由 lifespan 用
  `asyncio.create_task` 直接调起，**不经过 APScheduler** —— 首轮抓取可能几十秒，
  不能让首页等到超时（plan §11.1 第 6 步）。
- `daily_pipeline`：每天 `08:00`（`config.app.timezone`）出报，交给 APScheduler。
- `startup_coins` / `daily_coins`：铜币拍卖的补拉与每日任务，与知识聚合**各自独立**
  （spec §14.8/§14.9）：任一条挂掉不影响另一条。

不依赖 FastAPI；`app/main.py` 只负责把两者接进 lifespan。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import pipeline
from app.coins import pipeline as coins_pipeline
from app.config import Config
from app.pipeline import PipelineResult

logger = logging.getLogger(__name__)

DAILY_JOB_ID = "daily_pipeline"
DAILY_COINS_JOB_ID = "daily_coins"
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


async def startup_coins(config: Config) -> coins_pipeline.CoinsRunResult | None:
    """铜币启动补拉；失败只记日志，不让服务起不来（同 `startup_fetch` 的取舍）。"""
    if not config.coins.enabled:
        logger.info("铜币模块未启用（config.coins.enabled=false），跳过启动补拉")
        return None
    logger.info("铜币启动补拉开始")
    try:
        result = await coins_pipeline.run(config)
    except Exception:
        logger.exception("铜币启动补拉失败，等下一次定时任务重试")
        return None
    logger.info(
        "铜币启动补拉结束：status=%s 专场=%d 推荐=%d 查价=%d",
        result.status,
        result.sessions_stored,
        result.lots_recommended,
        result.lots_priced,
    )
    return result


async def daily_coins(config: Config) -> coins_pipeline.CoinsRunResult | None:
    """每天 08:00 的铜币任务；**与 `daily_pipeline` 互不传播失败**（spec §14.9）。

    就地兜住异常：配了 `coins.cdp_url` 时，那个 Chrome 没开着（或还没启动）是**正常情况**，
    不该把一坨堆栈丢给 APScheduler —— 只记一条能看懂的错误，明天再试。
    """
    if not config.coins.enabled:
        return None
    logger.info("定时任务触发：%s", DAILY_COINS_JOB_ID)
    try:
        return await coins_pipeline.run(config)
    except Exception as exc:  # noqa: BLE001 - 任何失败都只记日志，不影响知识日报
        logger.warning("铜币定时任务失败，等下一次触发重试：%s", exc)
        return None


def _cron_job(scheduler: AsyncIOScheduler, func, config: Config, job_id: str) -> None:
    scheduler.add_job(
        func,
        trigger="cron",
        hour=DAILY_HOUR,
        minute=0,
        args=[config],
        id=job_id,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        max_instances=1,
    )


def start(config: Config) -> AsyncIOScheduler:
    """建调度器、注册每日任务并启动。

    时区取 `config.app.timezone`，所以 `08:00` 是本地时间；`coalesce=True` 让「错过多次」
    只补跑一次，`max_instances=1` 让定时任务不与上一轮重叠（plan §11.2 / §18.9）。
    """
    scheduler = AsyncIOScheduler(timezone=config.app.tz)
    _cron_job(scheduler, daily_pipeline, config, DAILY_JOB_ID)
    if config.coins.enabled:
        _cron_job(scheduler, daily_coins, config, DAILY_COINS_JOB_ID)
    scheduler.start()
    logger.info(
        "调度器已启动：%s%s 每天 %02d:00（%s）",
        DAILY_JOB_ID,
        f" + {DAILY_COINS_JOB_ID}" if config.coins.enabled else "",
        DAILY_HOUR,
        config.app.timezone,
    )
    return scheduler

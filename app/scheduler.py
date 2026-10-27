"""APScheduler 任务定义（plan §11.2、§18.9）。

两个每日任务，跑之前都**先看功能开关**（管理页 /control 启停）：

- `daily_pipeline`：每天 `07:00`（`config.app.timezone`）出报；抓取与打分各自看开关。
- `daily_coins`：铜币的每日任务，采集与查价各自看开关；与知识聚合**互不传播失败**
  （spec §14.8/§14.9）：任一条挂掉不影响另一条。

**启动补拉已取消**（2026-10-15）：以前 lifespan 会用 `create_task` 跑一轮抓取与铜币采集，
重启一次就真去抓一遍、顺手把待打分条目烧一遍 token。现在重启不做任何事 ——
要立刻跑就去 `/control` 点「立即运行」，或用 CLI 命令。

不依赖 FastAPI；`app/main.py` 只负责把调度器接进 lifespan。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import switches, tasks
from app.coins import pipeline as coins_pipeline
from app.coins.pipeline import CoinsRunResult
from app.config import Config

logger = logging.getLogger(__name__)

DAILY_JOB_ID = "daily_pipeline"
DAILY_COINS_JOB_ID = "daily_coins"
DAILY_HOUR = 7
# 本机早上醒得晚，这一轮仍要执行而不是被丢弃（plan §11.2）
MISFIRE_GRACE_SECONDS = 3600


async def daily_pipeline(config: Config) -> None:
    """每天 07:00：按开关跑抓取与打分，两个都关就什么都不做。

    不吞异常：交给 APScheduler 记录（job 不会被摘掉），与改造前一致。
    """
    logger.info("定时任务触发：%s", DAILY_JOB_ID)

    if switches.is_enabled(switches.KNOWLEDGE_FETCH):
        logger.info("知识聚合·抓取：%s", await tasks.run_knowledge_fetch(config))
    else:
        logger.info("知识聚合·抓取已停用，跳过")

    if switches.is_enabled(switches.KNOWLEDGE_SCORE):
        logger.info("知识聚合·打分：%s", await tasks.run_knowledge_score(config))
    else:
        logger.info("知识聚合·打分已停用，跳过")


async def daily_coins(config: Config) -> CoinsRunResult | None:
    """每天 07:00 的铜币任务；**与 `daily_pipeline` 互不传播失败**（spec §14.9）。

    「采集 / 回采 / 热度回填 / 查价」四个开关各自决定跑不跑，顺序固定：
    先采新场（否则回采没有新数据），再回采终态，再扫 home.filter 回填热度，最后查价。
    逐段调用是为了让开关真正生效（合成一整轮会绕过回采开关）。

    就地兜住异常：网关一时不可用 / 限速被挡是**正常情况**，
    不该把一坨堆栈丢给 APScheduler —— 只记一条能看懂的错误，明天再试。
    """
    if not config.coins.enabled:
        return None

    collect = switches.is_enabled(switches.COINS_COLLECT)
    finalize = switches.is_enabled(switches.COINS_FINALIZE)
    scan = switches.is_enabled(switches.COINS_FILTER_SCAN)
    price = switches.is_enabled(switches.COINS_PRICE)
    if not (collect or finalize or scan or price):
        logger.info("铜币采集 / 回采 / 热度回填 / 查价都已停用，跳过 %s", DAILY_COINS_JOB_ID)
        return None

    logger.info(
        "定时任务触发：%s（采集=%s 回采=%s 热度回填=%s 查价=%s）",
        DAILY_COINS_JOB_ID,
        collect,
        finalize,
        scan,
        price,
    )
    try:
        result: CoinsRunResult | None = None
        if collect:
            result = await coins_pipeline.run(config, collect_only=True)
        if finalize:
            result = await coins_pipeline.run(config, finalize_only=True)
        if scan:
            logger.info("铜币·热度回填：%s", await tasks.run_coins_filter_scan(config))
        if price:
            result = await coins_pipeline.run(config, price_only=True)
        return result
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

    时区取 `config.app.timezone`，所以 `07:00` 是本地时间；`coalesce=True` 让「错过多次」
    只补跑一次，`max_instances=1` 让定时任务不与上一轮重叠（plan §11.2 / §18.9）。
    """
    scheduler = AsyncIOScheduler(timezone=config.app.tz)
    _cron_job(scheduler, daily_pipeline, config, DAILY_JOB_ID)
    if config.coins.enabled:
        _cron_job(scheduler, daily_coins, config, DAILY_COINS_JOB_ID)
    scheduler.start()
    logger.info(
        "调度器已启动：%s%s 每天 %02d:00（%s），是否执行以 /control 的开关为准",
        DAILY_JOB_ID,
        f" + {DAILY_COINS_JOB_ID}" if config.coins.enabled else "",
        DAILY_HOUR,
        config.app.timezone,
    )
    return scheduler

"""APScheduler 任务定义（plan §11.2、§18.9）。

两个每日任务，跑之前都**先看功能开关**（管理页 /control 启停）：

- `daily_pipeline`：每天 `07:00`（`config.app.timezone`）出报；抓取与打分各自看开关。
- `daily_coins`：铜币的每日任务，采集与查价各自看开关；与知识聚合**互不传播失败**
  （spec §14.8/§14.9）：任一条挂掉不影响另一条。

**全量深扫不再定时**（2026-11-01）：`daily_coins_deep` 已移除，需要时手工跑
`coins-filter-scan --full`。

**启动补拉已取消**（2026-10-15）：以前 lifespan 会用 `create_task` 跑一轮抓取与铜币采集，
重启一次就真去抓一遍、顺手把待打分条目烧一遍 token。现在重启不做任何事 ——
要立刻跑就去 `/control` 点「立即运行」，或用 CLI 命令。

不依赖 FastAPI；`app/main.py` 只负责把调度器接进 lifespan。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import repository, switches, tasks
from app.coins import pipeline as coins_pipeline
from app.coins.pipeline import CoinsRunResult
from app.config import Config

logger = logging.getLogger(__name__)

DAILY_JOB_ID = "daily_pipeline"
DAILY_COINS_JOB_ID = "daily_coins"
DAILY_HOUR = 7
# 本机早上醒得晚，这一轮仍要执行而不是被丢弃（plan §11.2）
MISFIRE_GRACE_SECONDS = 3600

# 铜币每日五段：段名（落库）/ 开关 / 日志用中文名；顺序固定，仍串行
_DAILY_COINS_STAGES: tuple[tuple[str, str, str], ...] = (
    ("collect", switches.COINS_COLLECT, "采集"),
    ("best_match", switches.COINS_BEST_MATCH, "好品配对"),
    ("finalize", switches.COINS_FINALIZE, "回采"),
    ("scan", switches.COINS_FILTER_SCAN, "热度回填"),
    ("price", switches.COINS_PRICE, "查价"),
)


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

    「采集 / 好品配对 / 回采 / 热度回填 / 查价」五段串行、各自独立：某段失败只记一条 warning
    并继续下一段；取消只把当前段记 `cancelled` 后继续向上传播。
    进度落 `coin_daily_runs` / `coin_daily_run_stages`，同日第二轮触发时跳过已 `ok` 的段，
    重启后由 `resume_incomplete` 只补未完成的段。
    """
    if not config.coins.enabled:
        return None

    if not any(switches.is_enabled(key) for _, key, _ in _DAILY_COINS_STAGES):
        logger.info("铜币采集 / 好品配对 / 回采 / 热度回填 / 查价都已停用，跳过 %s", DAILY_COINS_JOB_ID)
        return None

    logger.info("定时任务触发：%s", DAILY_COINS_JOB_ID)
    today = repository.now_local()[:10]
    run_id = repository.start_coin_daily_run(today)
    return await _run_daily_coins(config, run_id, today)


async def _run_daily_coins(config: Config, run_id: int, run_date: str) -> CoinsRunResult | None:
    """跑一条 run 里所有未完成且开关打开的段（四段串行）。

    `ok` 判定按 `run_date` 跨 run 聚合：同日已成功过的段即使换了 run 也不再调接口。
    """
    done = repository.coin_daily_done_stages(run_date)
    result: CoinsRunResult | None = None
    for stage, key, label in _DAILY_COINS_STAGES:
        if not switches.is_enabled(key):
            continue
        if stage in done:
            logger.info("铜币定时任务·%s 今日已完成，跳过（run %d）", label, run_id)
            continue
        stage_result = await _run_stage(config, run_id, stage, label)
        if stage_result is not None:
            result = stage_result
    repository.finish_coin_daily_run(run_id)
    return result


async def _run_stage(
    config: Config, run_id: int, stage: str, label: str
) -> CoinsRunResult | None:
    """跑一段并落状态；失败只记日志，取消记 `cancelled` 后继续抛。"""
    started = repository.now_local()
    try:
        result: CoinsRunResult | None = None
        if stage == "collect":
            result = await coins_pipeline.run(config, collect_only=True, skip_grade=True)
        elif stage == "best_match":
            logger.info("铜币·%s：%s", label, await tasks.run_coins_best_match(config))
        elif stage == "finalize":
            result = await coins_pipeline.run(config, finalize_only=True)
        elif stage == "scan":
            logger.info("铜币·%s：%s", label, await tasks.run_coins_filter_scan(config))
        else:
            result = await coins_pipeline.run(config, price_only=True)
    except asyncio.CancelledError:
        repository.finish_coin_daily_run_stage(
            run_id, stage, "cancelled", message="任务被取消", started_at=started
        )
        logger.warning("铜币定时任务·%s 被取消（run %d）", label, run_id)
        raise
    except Exception as exc:  # noqa: BLE001 - 一段失败不影响其余段
        repository.finish_coin_daily_run_stage(
            run_id, stage, "failed", message=str(exc), started_at=started
        )
        logger.warning("铜币定时任务·%s 失败，继续下一段：%s", label, exc)
        return None
    repository.finish_coin_daily_run_stage(run_id, stage, "ok", started_at=started)
    return result


def _past_daily_start(config: Config) -> bool:
    """是否已过 07:00（本地时区）：没到点不续跑。"""
    return datetime.now(config.app.tz).hour >= DAILY_HOUR


async def resume_incomplete(config: Config) -> None:
    """进程重启后补跑当日未完成的段（任务卡待确认 1-A）；跨日未完成一律 abandoned。"""
    if not config.coins.enabled:
        return
    run = repository.latest_incomplete_coin_daily_run()
    if run is None:
        return
    run_id = int(run["id"])  # type: ignore[arg-type]
    today = repository.now_local()[:10]
    if str(run["run_date"]) != today:
        repository.abandon_coin_daily_run(run_id)
        logger.info("铜币定时任务：%s 的 run %d 跨日未完成，标 abandoned", run["run_date"], run_id)
        return
    if not _past_daily_start(config):
        return
    logger.info("铜币定时任务：续跑当日 run %d 的未完成段落", run_id)
    await _run_daily_coins(config, run_id, today)


def _cron_job(
    scheduler: AsyncIOScheduler, func, config: Config, job_id: str, *, hour: int = DAILY_HOUR
) -> None:
    scheduler.add_job(
        func,
        trigger="cron",
        hour=hour,
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
        "调度器已启动：%s 每天 %02d:00（%s），是否执行以 /control 的开关为准",
        DAILY_JOB_ID,
        DAILY_HOUR,
        config.app.timezone,
    )
    if config.coins.enabled:
        logger.info(
            "铜币定时任务：%s 每天 %02d:00", DAILY_COINS_JOB_ID, DAILY_HOUR
        )
    return scheduler

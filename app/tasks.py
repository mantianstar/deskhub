"""七个可独立运行的功能：定时任务与管理页「立即运行」共用同一入口。

为什么单开一层：`app/pipeline.run` 是「抓取 → 打分」一体的（CLI 出报用），
而管理页要能**分开**跑抓取和打分，所以这里按功能拆成七个小函数。

手动触发（`trigger`）是**后台执行**：铜币查价一轮可能几分钟，
不能把 HTTP 请求挂在那里等；跑完把一句摘要留在 `last_state` 里供页面显示。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app import fetcher, pipeline, repository, scorer, switches
from app.coins import filter_scan as coins_filter_scan
from app.coins import matcher as coins_matcher
from app.coins import pipeline as coins_pipeline
from app.coins import recommend as coins_recommend
from app.config import Config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 单功能执行


async def run_knowledge_fetch(config: Config) -> str:
    """抓取所有启用的源并入库（不花 token）。"""
    outcomes = await fetcher.run_once(config)
    items_new = sum(outcome.items_new for outcome in outcomes)
    failed = pipeline.count_failed(tuple(outcomes))
    return f"抓取 {len(outcomes)} 个源：新增 {items_new} 条，失败 {failed} 个"


async def run_knowledge_score(config: Config) -> str:
    """给待打分条目打分（唯一花 token 的功能）。"""
    result = await scorer.run_pending(config)
    return (
        f"待打分 {result.pending} 条：成功 {result.items_scored} 条，"
        f"失败 {result.items_failed} 条，token {result.total_tokens}"
    )


async def run_coins_collect(config: Config) -> str:
    """采集专场 + 拍品并重算匹配（不查价）。"""
    result = await coins_pipeline.run(config, collect_only=True)
    return (
        f"专场入库 {result.sessions_stored} 个，拍品入库 {result.lots_stored} 条，"
        f"推荐 {result.lots_recommended} 条"
    )


async def run_coins_finalize(config: Config) -> str:
    """只回采已结束专场的拍品终态（不采集、不查价）。"""
    result = await coins_pipeline.run(config, finalize_only=True)
    return (
        f"回采 {result.sessions_finalized} 场：成交 {result.lots_sold} 条，"
        f"未成交 {result.lots_unsold} 条；成交库 session_final {result.sales_session_final} 条"
    )


async def run_coins_filter_scan(config: Config, *, full: bool = False) -> str:
    """扫 home.filter 给已入库拍品回填热度（只更新库里已有的拍品；`full=True` 走深扫续扫）。"""
    result = await coins_filter_scan.scan(config, full=full)
    if result.stopped == "cooldown":
        return f"home.filter 扫描冷却中（{coins_filter_scan.COOLDOWN_HOURS} 小时内限速），跳过"
    if result.status == "skipped":
        return f"home.filter 扫描跳过：{result.stopped}"
    return (
        f"home.filter 扫描 {result.cates} 个二级分类 / {result.pages} 页 / "
        f"{result.items} 条：回填 {result.updated} 条，未入库跳过 {result.missing} 条"
    )


async def run_coins_match(config: Config) -> str:
    """只重算拍品推荐位（不采集、不回采、不查价）。"""
    # 与 coins_pipeline.run 的匹配段同口径：收藏画像优先，没配画像回退标签匹配
    result = coins_recommend.rematch_all(config)
    if result.profiles_total:
        return (
            f"推荐重算：拍品 {result.lots_total} 条，命中 {result.lots_recommended} 条"
            f"（推荐项 {result.items_total} 个）"
        )
    legacy = coins_matcher.rematch_all(config)
    return (
        f"未配置收藏画像，回退标签匹配：拍品 {legacy.lots_total} 条，"
        f"命中 {legacy.lots_recommended} 条"
    )


async def run_coins_price(config: Config) -> str:
    """只给已入库的推荐拍品查价。"""
    result = await coins_pipeline.run(config, price_only=True)
    return (
        f"已查价 {result.lots_priced} 条，当日跳过 {result.lots_pricing_skipped} 条，"
        f"推迟 {result.lots_pricing_deferred} 条"
    )


RUNNERS: dict[str, Callable[[Config], Awaitable[str]]] = {
    switches.KNOWLEDGE_FETCH: run_knowledge_fetch,
    switches.KNOWLEDGE_SCORE: run_knowledge_score,
    switches.COINS_COLLECT: run_coins_collect,
    switches.COINS_FINALIZE: run_coins_finalize,
    switches.COINS_FILTER_SCAN: run_coins_filter_scan,
    switches.COINS_MATCH: run_coins_match,
    switches.COINS_PRICE: run_coins_price,
}


# ---------------------------------------------------------------- 手动触发与状态


@dataclass(frozen=True)
class RunState:
    """一次手动运行的结局，供管理页显示。"""

    status: str  # ok / error
    message: str
    finished_at: str


_running: dict[str, asyncio.Task[None]] = {}
_last_state: dict[str, RunState] = {}


def is_running(key: str) -> bool:
    task = _running.get(key)
    return task is not None and not task.done()


def last_state(key: str) -> RunState | None:
    return _last_state.get(key)


def trigger(config: Config, key: str) -> bool:
    """手动跑一次（**忽略开关**），后台执行。

    返回是否真的触发：同一功能已在跑时返回 `False`，避免连点把任务叠起来。
    未知 key 抛 `ValueError`。
    """
    if key not in RUNNERS:
        raise ValueError(f"未知功能：{key}")
    if is_running(key):
        return False
    _running[key] = asyncio.create_task(_run(config, key))
    return True


async def _run(config: Config, key: str) -> None:
    """后台跑一个功能：异常就地兜住（没人 await 它），结局记进 `_last_state`。"""
    try:
        message = await RUNNERS[key](config)
        _last_state[key] = RunState("ok", message, repository.utcnow())
        logger.info("手动运行完成 %s：%s", key, message)
    except Exception as exc:  # noqa: BLE001 - 后台任务，异常只能在这里收掉
        _last_state[key] = RunState("error", str(exc), repository.utcnow())
        logger.warning("手动运行失败 %s：%s", key, exc)
    finally:
        _running.pop(key, None)

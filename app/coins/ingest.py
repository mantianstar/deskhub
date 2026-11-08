"""异步采集队列执行器（spec §14.18 采集中心）。

页面只负责「入队」（`repository.enqueue_coin_ingest_task`），真正的采集在这里按队列顺序跑：
一个任务 = 一个 Numista 系列，4 个节点（系列 / 年份 / 估值 / 图片）各自记状态。

三条口径（2026-11-08 与用户确认）：
1. 同系列已在 pending / running 时复用现有任务，不重复入队（在 repository 层）；
2. 节点异常 → 该节点 `failed` + `error`，任务收尾 `failed`，后续节点保持 `wait`；
3. 图片节点调用成功但没有图 → `done`（不算缺失）。

为什么后台跑：单节点的采集耗时不可控（类型页 / CDP 下载甚至几分钟），
且估值与图片走同一条 Chrome CDP 链路，不能并发 —— 队列串行最稳。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app import repository
from app.coins import explore as coins_explore
from app.coins import numista as coins_numista
from app.coins import numista_web as coins_numista_web
from app.config import Config
from app.models import CoinIngestTask, CoinSeries

logger = logging.getLogger(__name__)

NodeRunner = Callable[[Config, CoinIngestTask], Awaitable[str]]


def series_task_name(series: CoinSeries | dict[str, object] | None) -> str:
    """入队任务名口径：国家 · 执政者 · 面值 · 年份（区间）。

    `series` 可以是本地系列（CoinSeries），也可以是探索候选池里的 payload（尚未落库）。
    字段缺失跳过；一个字段都没有时回退系列标题，再没有就留空（页面显示 Numista #id）。
    """
    if series is None:
        return ""

    def value(key: str) -> object:
        if isinstance(series, dict):
            return series.get(key)
        return getattr(series, key, None)

    parts = [
        str(text).strip()
        for text in (value("issuer"), value("ruler"), value("denomination"))
        if text and str(text).strip()
    ]
    min_year, max_year = value("min_year"), value("max_year")
    if min_year:
        parts.append(
            f"{min_year}–{max_year}"
            if max_year and max_year != min_year
            else str(min_year)
        )
    title = value("title")
    return " · ".join(parts) or (str(title) if title else "")


# ---------------------------------------------------------------- 4 个节点


async def _node_series(config: Config, task: CoinIngestTask) -> str:
    """系列详情：本地已有就跳过；探索时暂存在候选池的直接用，不再调接口。"""
    if repository.get_coin_series(task.series_numista_id) is not None:
        return "skip"
    if repository.get_coin_explore_candidate(task.series_numista_id) is not None:
        coins_explore.adopt_candidate(task.series_numista_id, mark_adopted=False)
        return "done"
    await coins_numista.collect_series_by_id(config, series_id=task.series_numista_id)
    return "done"


async def _node_years(config: Config, task: CoinIngestTask) -> str:
    """年份（单版）：该系列已有单版就跳过。"""
    series = repository.get_coin_series(task.series_numista_id)
    if series is not None and repository.list_coin_issues(series.id):
        return "skip"
    await coins_numista.collect_issues(config, series_id=task.series_numista_id)
    return "done"


async def _node_values(config: Config, task: CoinIngestTask) -> str:
    """估值 + Frequency（类型页，走 CDP）：该系列已有估值就跳过。"""
    if repository.list_coin_issue_values(task.series_numista_id):
        return "skip"
    await coins_numista_web.collect_issue_values(config, series_id=task.series_numista_id)
    return "done"


async def _node_images(config: Config, task: CoinIngestTask) -> str:
    """正 / 反图本地缓存：两面都在就跳过；采完仍无图也记 `done`。"""
    if all(
        coins_numista_web.cached_image(task.series_numista_id, side)
        for side in coins_numista_web.IMAGE_SIDES
    ):
        return "skip"
    await coins_numista_web.download_series_images(
        config, numista_id=task.series_numista_id
    )
    return "done"


NODES: tuple[tuple[str, NodeRunner], ...] = (
    ("series", _node_series),
    ("years", _node_years),
    ("values", _node_values),
    ("images", _node_images),
)


# ---------------------------------------------------------------- 执行


async def run_task(config: Config, task: CoinIngestTask) -> str:
    """跑一个任务的 4 个节点，返回任务结局（`done` / `failed`）。

    节点异常就地收掉：记 `failed` + error 并收尾任务，后续节点不再跑（保持 `wait`）。
    """
    for node, runner in NODES:
        repository.set_coin_ingest_node(task.id, node, "running")
        try:
            state = await runner(config, task)
        except Exception as exc:  # noqa: BLE001 - 节点异常都归到任务行上，供采集中心展示
            logger.warning("采集任务 %s 节点 %s 失败：%s", task.id, node, exc)
            repository.set_coin_ingest_node(task.id, node, "failed", error=str(exc))
            repository.finish_coin_ingest_task(task.id, "failed", error=str(exc))
            return "failed"
        repository.set_coin_ingest_node(task.id, node, state)
    repository.finish_coin_ingest_task(task.id, "done")
    return "done"


async def run_queue(config: Config) -> int:
    """把队列里的 pending 任务依次跑完，返回本轮处理的任务数。"""
    handled = 0
    while (task := repository.claim_next_coin_ingest_task()) is not None:
        await run_task(config, task)
        handled += 1
    return handled


_running: asyncio.Task[None] | None = None


def is_running() -> bool:
    return _running is not None and not _running.done()


def start(config: Config) -> bool:
    """后台跑一轮队列；已在跑时返回 `False`（避免连点叠起来）。"""
    global _running
    if is_running():
        return False
    _running = asyncio.create_task(_run_queue(config))
    return True


async def _run_queue(config: Config) -> None:
    """后台入口：异常就地兜住（没人 await 它），只留日志。"""
    try:
        handled = await run_queue(config)
        logger.info("采集队列完成：处理 %d 个任务", handled)
    except Exception as exc:  # noqa: BLE001 - 后台任务，异常只能在这里收掉
        logger.warning("采集队列异常：%s", exc)
    finally:
        global _running
        _running = None

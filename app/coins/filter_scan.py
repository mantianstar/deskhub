"""`home.filter` 扫描：给**已入库**拍品回填热度 / 中拍人 / 属性标签（需求 B）。

三条口径先定死：

- 只扫硬币的二级分类（`cate_id_1=11000` 下的 `cate_id_2`）：只传一级会被
  `totalSize` 的 10000 截断值卡住，必须按二级分类分开扫；
- 只更新已入库拍品（`mxiqi_id = "{auction_id}:{item_id}"`）：filter 是全站池，
  不在我们专场里的条目直接丢弃并计数；
- 不回写 `outcome` / `final_price` / `closed_at`：终态口径归 `pipeline._finalize`。

风控：串行 + 页间隔抖动、日配额、429/403 冷却 6 小时、单实例。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import repository
from app.coins.gapi import SOLD_STATUS, GapiError, MxiqiGapi, to_utc_iso
from app.config import Config
from app.models import CoinFetchRun, CoinFetchStatus

logger = logging.getLogger(__name__)

RUN_KIND = "home_filter"
PROVIDER = "mxiqi_filter"
PARENT_CODE = "11000"
PAGES_PER_CATE = 20
FULL_MAX_PAGES = 500
PAGE_INTERVAL_SECONDS = 0.5
PAGE_JITTER_SECONDS = 0.2
REQUEST_RETRIES = 2
DAILY_QUOTA = 300
COOLDOWN_HOURS = 6
COOLDOWN_STATUSES = (403, 429)
INCREMENTAL_SLACK_HOURS = 1
STALE_PAGES_TO_STOP = 2

_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_running = False


@dataclass(frozen=True)
class ScanResult:
    """一轮扫描的汇总，供任务日志与管理页显示。"""

    status: str
    pages: int = 0
    items: int = 0
    updated: int = 0
    missing: int = 0
    cates: int = 0
    stopped: str | None = None


class _CooldownError(RuntimeError):
    """429 / 403：立即停，落冷却记录。"""

    def __init__(self, status: int) -> None:
        super().__init__(f"网关返回 {status}，进入冷却")
        self.status = status


class _QuotaExceeded(RuntimeError):
    """当日请求配额用尽。"""


# ---------------------------------------------------------------- 字段映射（纯函数）


def _whole(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def tags_from_attrs(attrs_aggs: object) -> str | None:
    """`attrs_aggs` → tags JSON；空 / 无有效项返回 None（不覆盖库里已有标签）。"""
    if not isinstance(attrs_aggs, list):
        return None
    tags: dict[str, str] = {}
    for attr in attrs_aggs:
        if not isinstance(attr, dict):
            continue
        name = str(attr.get("attr_name") or "").strip()
        value = str(attr.get("attr_value") or "").strip()
        if name and value:
            tags[name] = value
    return json.dumps(tags, ensure_ascii=False) if tags else None


def bid_users_json(raw: object) -> str | None:
    """`bid_users`（uid 整数数组）→ JSON 数组；空数组返回 None。"""
    if not isinstance(raw, list):
        return None
    uids = [uid for uid in (_whole(value) for value in raw) if uid is not None]
    return json.dumps(uids) if uids else None


def update_row(item: dict) -> dict[str, object] | None:
    """filter 条目 → 回填行；缺 `auction_id` / `item_id` 时返回 None（定位不到拍品）。"""
    auction_id = item.get("auction_id")
    item_id = item.get("item_id")
    if auction_id is None or item_id is None:
        return None
    winner = item.get("won_user_info")
    if not isinstance(winner, dict):
        winner = {}
    uid = winner.get("uid")
    nickname = str(winner.get("nickname") or "").strip() or None
    return {
        "mxiqi_id": f"{auction_id}:{item_id}",
        "bid_num": _whole(item.get("bid_num")),
        "bid_user_num": _whole(item.get("bid_user_num")),
        "bid_users": bid_users_json(item.get("bid_users")),
        "winner_uid": str(uid) if uid is not None else None,
        "winner_nickname": nickname,
        "tags": tags_from_attrs(item.get("attrs_aggs")),
    }


# ---------------------------------------------------------------- 时间 / 风控辅助


def _hours_ago_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(_UTC_FORMAT)


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _day_start(config: Config) -> str:
    """业务时区当天 00:00（UTC ISO）：日配额按本地日期切。"""
    now = datetime.now(config.app.tz)
    return repository.to_utc_str(
        now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    )


def _home_filter_runs(limit: int = 50) -> list[CoinFetchRun]:
    return [run for run in repository.latest_coin_fetch_runs(limit) if run.kind == RUN_KIND]


def _cooldown_until() -> str | None:
    """最近一次 home.filter 若是 429/403，返回冷却结束时间（仍在冷却期内）。"""
    runs = _home_filter_runs()
    if not runs:
        return None
    latest = runs[0]
    if latest.status != CoinFetchStatus.HTTP_ERROR or latest.http_status not in COOLDOWN_STATUSES:
        return None
    finished = _parse_utc(latest.finished_at)
    if finished is None:
        return None
    until = finished + timedelta(hours=COOLDOWN_HOURS)
    if until <= datetime.now(timezone.utc):
        return None
    return until.strftime(_UTC_FORMAT)


def _incremental_cutoff() -> str | None:
    """上次成功扫描开始时间 - 1h；没有历史就不设停止条件。"""
    for run in _home_filter_runs():
        if run.status != CoinFetchStatus.OK:
            continue
        started = _parse_utc(run.started_at)
        if started is None:
            return None
        return (started - timedelta(hours=INCREMENTAL_SLACK_HOURS)).strftime(_UTC_FORMAT)
    return None


def _record_run(
    started_at: str,
    status: str,
    *,
    items_new: int = 0,
    error: str | None = None,
    http_status: int | None = None,
    target: str | None = None,
) -> None:
    repository.record_coin_fetch_run(
        CoinFetchRun(
            id=None,
            kind=RUN_KIND,
            target=target,
            started_at=started_at,
            finished_at=repository.utcnow(),
            status=status,
            http_status=http_status,
            items_new=items_new,
            error=error,
        )
    )


def _http_status(exc: GapiError) -> int | None:
    """`MxiqiGapi._call` 把 `httpx.HTTPStatusError` 包成 `GapiError`，状态码在 cause 上。"""
    response = getattr(exc.__cause__, "response", None)
    return getattr(response, "status_code", None)


async def _pause() -> None:
    delay = PAGE_INTERVAL_SECONDS + random.uniform(-PAGE_JITTER_SECONDS, PAGE_JITTER_SECONDS)
    if delay > 0:
        await asyncio.sleep(delay)


async def _fetch_page(
    api: MxiqiGapi,
    search_params: dict,
    page: int,
    budget: Callable[[], None],
) -> list[dict]:
    """取一页；429/403 抛 `_CooldownError`，其余错误重试 2 次后抛出（调用方跳过该页）。"""
    last: GapiError | None = None
    for attempt in range(REQUEST_RETRIES + 1):
        budget()
        try:
            return await api.search_filter_items(search_params, page)
        except GapiError as exc:
            last = exc
            status = _http_status(exc)
            if status in COOLDOWN_STATUSES:
                raise _CooldownError(status) from exc
            if attempt < REQUEST_RETRIES:
                await _pause()
    assert last is not None
    raise last


def _page_all_stale(raw_items: list[dict], cutoff: str, tz) -> bool:
    """整页条目的 `update_time` 都早于 cutoff 才算「旧页」。"""
    if not raw_items:
        return False
    for item in raw_items:
        stamp = to_utc_iso(item.get("update_time"), tz)
        if stamp is None or stamp >= cutoff:
            return False
    return True


# ---------------------------------------------------------------- 扫描主流程


async def scan(
    config: Config, *, pages_per_cate: int = PAGES_PER_CATE, full: bool = False
) -> ScanResult:
    """扫硬币二级分类的 `home.filter`，回填已入库拍品的热度。

    `full=True` 扫到空页（约 3000+ 页，受日配额约束）；默认每个二级分类只扫前
    `pages_per_cate` 页。增量模式用「上次成功扫描开始时间 - 1h」做停止条件。
    """
    global _running
    if _running:
        return ScanResult(status="skipped", stopped="running")
    _running = True
    started = repository.utcnow()
    try:
        return await _scan(config, started, pages_per_cate=pages_per_cate, full=full)
    finally:
        _running = False


async def _scan(
    config: Config, started: str, *, pages_per_cate: int, full: bool
) -> ScanResult:
    cooldown_until = _cooldown_until()
    if cooldown_until is not None:
        logger.warning("home.filter 扫描冷却中（至 %s），本次跳过", cooldown_until)
        return ScanResult(status="skipped", stopped="cooldown")

    cates = [row["code"] for row in repository.list_coin_categories(PARENT_CODE)]
    if not cates:
        logger.warning("home.filter 扫描：分类字典没有 parent_code=%s 的二级分类", PARENT_CODE)
        return ScanResult(status="skipped", stopped="no_categories")

    cutoff = None if full else _incremental_cutoff()
    used = repository.count_api_calls_since(PROVIDER, _day_start(config))

    def budget() -> None:
        nonlocal used
        if used >= DAILY_QUOTA:
            raise _QuotaExceeded
        used += 1
        repository.record_api_call(PROVIDER)

    pages = items = updated = missing = 0
    stopped: str | None = None
    seen_items: set[object] = set()
    limit = FULL_MAX_PAGES if full else pages_per_cate

    api = MxiqiGapi(config)
    await api.__aenter__()
    try:
        for cate in cates:
            search_params = {
                "cate_id_1": int(PARENT_CODE),
                "cate_id_2": int(cate),
                "status": SOLD_STATUS,
            }
            stale_pages = 0
            for page in range(1, limit + 1):
                try:
                    raw_items = await _fetch_page(api, search_params, page, budget)
                except _QuotaExceeded:
                    stopped = "quota"
                    logger.warning("home.filter 扫描：日配额 %d 次用尽，提前结束", DAILY_QUOTA)
                    break
                except _CooldownError as exc:
                    _record_run(
                        started,
                        CoinFetchStatus.HTTP_ERROR,
                        http_status=exc.status,
                        error=str(exc),
                        target=_target(cates, pages, items, missing),
                    )
                    logger.warning("home.filter 扫描被限速（%d），冷却 %d 小时", exc.status, COOLDOWN_HOURS)
                    return ScanResult(
                        status=CoinFetchStatus.HTTP_ERROR,
                        pages=pages,
                        items=items,
                        updated=updated,
                        missing=missing,
                        cates=len(cates),
                        stopped="cooldown",
                    )
                except GapiError as exc:
                    stopped = "page_error"
                    logger.warning("home.filter 第 %d 页失败，跳过：%s", page, exc)
                    await _pause()
                    continue

                if not raw_items:
                    break

                pages += 1
                rows: list[dict[str, object]] = []
                for item in raw_items:
                    items += 1
                    item_key = item.get("item_id")
                    if item_key is not None:
                        if item_key in seen_items:
                            continue
                        seen_items.add(item_key)
                    row = update_row(item)
                    if row is not None:
                        rows.append(row)
                if rows:
                    done, absent = repository.update_coin_lot_filter_heat(rows)
                    updated += done
                    missing += absent

                if cutoff is not None:
                    stale_pages = stale_pages + 1 if _page_all_stale(raw_items, cutoff, config.app.tz) else 0
                    if stale_pages >= STALE_PAGES_TO_STOP:
                        stopped = "incremental"
                        break

                await _pause()
            if stopped == "quota":
                break
    finally:
        await api.__aexit__()

    target = _target(cates, pages, items, missing)
    _record_run(
        started,
        CoinFetchStatus.OK,
        items_new=updated,
        error="日配额用尽" if stopped == "quota" else None,
        target=target,
    )
    logger.info(
        "home.filter 扫描完成：%d 个二级分类 / %d 页 / %d 条，回填 %d 条，未入库跳过 %d 条",
        len(cates),
        pages,
        items,
        updated,
        missing,
    )
    return ScanResult(
        status=CoinFetchStatus.OK,
        pages=pages,
        items=items,
        updated=updated,
        missing=missing,
        cates=len(cates),
        stopped=stopped,
    )


def _target(cates: list[str], pages: int, items: int, missing: int) -> str:
    return f"cates={len(cates)} pages={pages} items={items} missing={missing}"

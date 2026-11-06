"""`home.filter` 扫描：给**已入库**拍品回填热度 / 中拍人 / 属性标签（需求 B）。

三条口径先定死：

- 只扫硬币的二级分类（`cate_id_1=11000` 下的 `cate_id_2`）：只传一级会被
  `totalSize` 的 10000 截断值卡住，必须按二级分类分开扫；
- 只更新已入库拍品（`mxiqi_id = "{auction_id}:{item_id}"`）：filter 是全站池，
  不在我们专场里的条目直接丢弃并计数；
- 不回写 `outcome` / `final_price` / `closed_at`：终态口径归 `pipeline._finalize`。

风控：串行 + 页间隔抖动、日配额、429/403 冷却 6 小时、单实例。
全量模式把当日剩余配额拆三段：顶部（页数按近 `TOP_LOOKBACK_DAYS` 日成交估算；距上次
成功扫描不足 `TOP_REFRESH_HOURS` 时只留保活页）、昨日回补（从顶部窗口往后扫到本地
昨天 0 点，每天只跑一次，不动深扫断点）、深扫（按二级分类断点续扫，缺口大者多分页，
分到但无可用区间的页再二次回收）。站点 `totalSize` 封顶 10,000，单类最多 476 页。
深扫按未覆盖成交数分页续扫，直到空页或 `FULL_MAX_PAGES` 底；空页记 `DEEP_DONE_PAGE`，
之后若仍有缺口，隔 `DEEP_RESCAN_HOURS` 从第 1 页重扫一轮（新成交只会出现在头部）。
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
from app.coins.gapi import SOLD_STATUS, GapiError, MxiqiGapi
from app.config import Config
from app.models import CoinFetchRun, CoinFetchStatus

logger = logging.getLogger(__name__)

RUN_KIND = "home_filter"
PROVIDER = "mxiqi_filter"
PARENT_CODE = "11000"
PAGES_PER_CATE = 20  # 顶部窗口固定页数（home.filter 按 update_time 降序）
TOP_REFRESH_HOURS = 6  # 距上次成功扫描不足该时长时，顶部只留保活页
TOP_KEEPALIVE_PAGES = 1  # 顶部保活页数下限（刚扫过时按新增成交估页，保底这么多页）
MIN_DEEP_PAGES = 5  # 深扫按未覆盖成交比例分配时，每个分类保底页数
FULL_MAX_PAGES = 500  # 站点 totalSize 封顶 10000 / 21 条每页 = 476 页，500 只是兜底
DEEP_DONE_PAGE = FULL_MAX_PAGES + 1  # 深扫已到底的断点标记：再往后没有可回填的成交
DEEP_RESCAN_HOURS = 72  # 已扫到底但仍有缺口时，隔这么久从第 1 页重扫一轮
ITEMS_PER_PAGE = 21  # 站点每页条数（按成交数估算顶部页数）
PAGE_INTERVAL_SECONDS = 0.5
PAGE_JITTER_SECONDS = 0.2
REQUEST_RETRIES = 2
DAILY_QUOTA = 1200
COOLDOWN_HOURS = 6
COOLDOWN_STATUSES = (403, 429)
NO_GAIN_PAGES_TO_STOP = 10  # 增量扫连续这么多页没有库内命中就停（update_time 不是成交时间，不能按它判旧）
PRIORITY_CATEGORY_CODES = ("11070", "11050", "11020")  # 增量模式优先扫的二级分类
INCREMENTAL_CURSOR_PREFIX = "incremental:"  # 增量断点单独存，避免覆盖全量深扫断点

_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S"
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
    return repository.to_local_str(datetime.now(timezone.utc) - timedelta(hours=hours))


def _parse_utc(value: str | None) -> datetime | None:
    """存储串 → 带北京时区的 datetime（旧 `Z` 串也认）。"""
    parsed = repository.parse_dt(value)
    return parsed.replace(tzinfo=repository.BEIJING) if parsed is not None else None


def _day_start(config: Config) -> str:
    """业务时区当天 00:00（北京时间存储格式）：日配额按本地日期切。"""
    now = datetime.now(config.app.tz)
    return repository.to_local_str(
        now.replace(hour=0, minute=0, second=0, microsecond=0)
    )


def allocate_pages(weights: dict[str, int], budget: int, *, minimum: int = 0) -> dict[str, int]:
    """按权重把 `budget` 页分给各分类（最大余数法），每类先保底 `minimum` 页。"""
    cates = list(weights)
    if not cates or budget <= 0:
        return {cate: 0 for cate in cates}
    floor_min = min(minimum, budget // len(cates))
    pages = {cate: floor_min for cate in cates}
    rest = budget - floor_min * len(cates)
    if rest <= 0:
        return pages
    total = sum(max(0, weights[cate]) for cate in cates)
    if total > 0:
        shares = {cate: rest * max(0, weights[cate]) / total for cate in cates}
    else:  # 昨日没有成交：均分
        shares = {cate: rest / len(cates) for cate in cates}
    for cate in cates:
        pages[cate] += int(shares[cate])
    leftover = rest - sum(int(shares[cate]) for cate in cates)
    for cate in sorted(cates, key=lambda c: shares[c] - int(shares[c]), reverse=True)[:leftover]:
        pages[cate] += 1
    return pages


def _gap_weights() -> dict[str, int]:
    """各二级分类尚未回填出价人数的成交拍品数，作为深扫配额权重（缺口越大多分页）。"""
    return repository.count_uncovered_sold_by_category()


def _reclaim_pages(pages: dict[str, int], room: dict[str, int]) -> dict[str, int]:
    """把超出各类可用区间（`room`）的页收回，再按余量比例转给还有空间的分类。"""
    capped = {cate: min(n, max(room.get(cate, 0), 0)) for cate, n in pages.items()}
    for _ in range(len(capped) + 1):
        surplus = sum(pages.values()) - sum(capped.values())
        spare = {
            cate: room.get(cate, 0) - capped[cate]
            for cate in capped
            if room.get(cate, 0) > capped[cate]
        }
        if surplus <= 0 or not spare:
            break
        extra = allocate_pages(spare, min(surplus, sum(spare.values())), minimum=0)
        capped = {cate: capped[cate] + extra.get(cate, 0) for cate in capped}
    return capped


def _last_ok_started() -> str | None:
    """最近一次成功扫描的开始时间；没有成功记录时返回 None。"""
    for run in _home_filter_runs():
        if run.status == CoinFetchStatus.OK:
            return run.started_at
    return None


def _top_refresh_due() -> bool:
    """距上次成功扫描是否已超过 `TOP_REFRESH_HOURS`（不足则顶部不必整段重扫）。"""
    started_at = _last_ok_started()
    if started_at is None:
        return True
    started = _parse_utc(started_at)
    if started is None:
        return True
    return datetime.now(repository.BEIJING) - started >= timedelta(hours=TOP_REFRESH_HOURS)


def _keepalive_pages(cates: list[str], cap: int) -> dict[str, int]:
    """刚扫过时的顶部页数：按上次成功扫描后的成交数估页，保底 `TOP_KEEPALIVE_PAGES` 页。"""
    since = _last_ok_started() or _hours_ago_iso(24)
    recent = repository.count_sold_by_category_since(since)
    return {
        cate: min(
            cap,
            max(
                TOP_KEEPALIVE_PAGES,
                (recent.get(cate, 0) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE,
            ),
        )
        for cate in cates
    }


def _rescan_due(progress: tuple[int, str] | None) -> bool:
    """已到底的分类是否到了重扫时间：没有进度或更新时间读不出来也算到期。"""
    if progress is None:
        return True
    updated = _parse_utc(progress[1])
    if updated is None:
        return True
    return datetime.now(repository.BEIJING) - updated >= timedelta(hours=DEEP_RESCAN_HOURS)


def _log_allocation(
    cates: list[str],
    available: int,
    top_pages: dict[str, int],
    deep_pages: dict[str, int],
    resume_at: dict[str, int],
) -> None:
    """把本轮两段配额按二级分类列出来，便于核对分配是否合理。"""
    logger.info(
        "home.filter 分段配额（可用 %d 页）：顶部 %d ｜ 深扫 %d",
        available,
        sum(top_pages.values()),
        sum(deep_pages.values()),
    )
    for cate in cates:
        logger.info(
            "  分类配额 %s：顶部 %d ｜ 深扫 %d（断点 %s）",
            cate,
            top_pages[cate],
            deep_pages.get(cate, 0),
            resume_at.get(cate, 1),
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
    if until <= datetime.now(repository.BEIJING):
        return None
    return until.strftime(_UTC_FORMAT)


def _priority_cates(cates: list[str]) -> list[str]:
    """增量模式的类目顺序：11070 → 11050 → 11020 优先，其余保持原顺序。"""
    head = [code for code in PRIORITY_CATEGORY_CODES if code in cates]
    return head + [code for code in cates if code not in head]


def _yesterday_gap(config: Config) -> dict[str, int]:
    """昨日成交里仍缺 `bid_num` 的分类缺口（为 0 才切下一个二级分类）。"""
    now = datetime.now(config.app.tz)
    start, end = repository.day_bounds_local(
        config.app.tz, (now - timedelta(days=1)).date()
    )
    return repository.count_uncovered_sold_by_category_between(start, end)


def _incremental_resume_page(
    progress: dict[str, tuple[int, str]], key: str, today: str
) -> int:
    """增量断点：同一业务日内的页码才续用，跨天回到第 1 页。"""
    entry = progress.get(key)
    if entry is None:
        return 1
    page, updated_at = entry
    if str(updated_at)[:10] != today or page < 1:
        return 1
    return page


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
            finished_at=repository.now_local(),
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


# ---------------------------------------------------------------- 扫描主流程


async def scan(
    config: Config, *, pages_per_cate: int = PAGES_PER_CATE, full: bool = False
) -> ScanResult:
    """扫硬币二级分类的 `home.filter`，回填已入库拍品的热度。

    `full=True` 把当日剩余配额拆三段：顶部（页数按近 `TOP_LOOKBACK_DAYS` 日成交估算，
    距上次成功扫描不足 `TOP_REFRESH_HOURS` 时只留保活页）、昨日回补（扫到本地昨天 0 点，
    每天一次，不动深扫断点）、深扫（按未回填成交数分页续扫，余量二次回收）；配额用尽后
    下一轮从断点继续，扫到空页或 `FULL_MAX_PAGES` 底。增量模式按
    11070 → 11050 → 11020 → 其他 的优先级逐类扫，某类昨日成交的 `bid_num` 缺口归零
    才切下一个类目；连续 `NO_GAIN_PAGES_TO_STOP` 页没有库内命中即停，类目断点存
    `incremental:` 前缀，重跑从断点续扫。
    """
    global _running
    if _running:
        return ScanResult(status="skipped", stopped="running")
    _running = True
    started = repository.now_local()
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
    if not full:
        cates = _priority_cates(cates)
    if not cates:
        logger.warning("home.filter 扫描：分类字典没有 parent_code=%s 的二级分类", PARENT_CODE)
        return ScanResult(status="skipped", stopped="no_categories")

    used = repository.count_api_calls_since(PROVIDER, _day_start(config))
    top_pages = {cate: pages_per_cate for cate in cates}
    if full and not _top_refresh_due():
        # 刚扫过：顶部只补上次成功扫描后的新增成交，按量估页（不低于保活页）
        top_pages = _keepalive_pages(cates, pages_per_cate)

    progress = repository.coin_filter_scan_progress()
    resume_at = {code: next_page for code, (next_page, _) in progress.items()}
    available = max(DAILY_QUOTA - used, 0)
    deep_pages: dict[str, int] = {}
    if full:
        if sum(top_pages.values()) > available:
            top_pages = allocate_pages(top_pages, available, minimum=1)
        # 只深扫还有未回填成交的分类：缺口为 0 的类不再占配额
        gap = _gap_weights()
        weights = {cate: gap[cate] for cate in cates if gap.get(cate, 0) > 0}
        # 已扫到底但仍有缺口：冷却到期后从第 1 页重扫（新成交只会出现在头部）
        for cate in weights:
            if resume_at.get(cate, 1) >= DEEP_DONE_PAGE and _rescan_due(progress.get(cate)):
                logger.info(
                    "分类 %s 已扫到底但仍有 %d 条未回填，断点重置为 1 重扫", cate, weights[cate]
                )
                resume_at[cate] = 1
                repository.set_coin_filter_scan_page(cate, 1)
        # 每类可用深扫区间：断点越靠后能分的页越少，分出去的会二次回收
        room = {
            cate: max(
                FULL_MAX_PAGES - max(resume_at.get(cate, 1), top_pages[cate] + 1) + 1, 0
            )
            for cate in weights
        }
        # 接近封顶但仍可续扫的类：至少留保底页，别把权重整个收走
        for cate, n in room.items():
            if n < MIN_DEEP_PAGES and resume_at.get(cate, 1) < DEEP_DONE_PAGE:
                room[cate] = MIN_DEEP_PAGES
        pool = max(available - sum(top_pages.values()), 0)
        deep_pages = _reclaim_pages(
            allocate_pages(weights, pool, minimum=MIN_DEEP_PAGES), room
        )
        _log_allocation(cates, available, top_pages, deep_pages, resume_at)

    def budget() -> None:
        nonlocal used
        if used >= DAILY_QUOTA:
            raise _QuotaExceeded
        used += 1
        repository.record_api_call(PROVIDER)

    pages = items = updated = missing = 0
    stopped: str | None = None
    seen_items: set[object] = set()

    api = MxiqiGapi(config)
    await api.__aenter__()
    try:
        phases = ("top", "deep") if full else ("top",)
        today = started[:10]
        for phase in phases:
            for cate in cates:
                cursor_key = f"{INCREMENTAL_CURSOR_PREFIX}{cate}"
                if not full:
                    # 增量：昨日成交的 bid_num 已无缺口就跳过本类；否则按断点续扫
                    if _yesterday_gap(config).get(cate, 0) == 0:
                        repository.set_coin_filter_scan_page(cursor_key, 0)
                        continue
                    start_page = _incremental_resume_page(progress, cursor_key, today)
                search_params = {
                    "cate_id_1": int(PARENT_CODE),
                    "cate_id_2": int(cate),
                    "status": SOLD_STATUS,
                }
                no_gain_pages = 0
                if phase == "top":  # 顶部窗口：最新拍品在最前面
                    page_nums = (
                        range(1, top_pages[cate] + 1)
                        if full
                        else range(start_page, FULL_MAX_PAGES + 1)
                    )
                else:  # 深扫：从断点续扫，只扫本类分到的页数
                    start = max(resume_at.get(cate, 1), top_pages[cate] + 1)
                    stop = min(FULL_MAX_PAGES, start + deep_pages.get(cate, 0) - 1)
                    page_nums = range(start, stop + 1)
                for page in page_nums:
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
                        if not full:
                            # 增量按断点顺序推进，跳过失败页再往后扫没有意义；站点封顶后
                            # 第 477 页起恒报「拍品查询失败」，继续扫到 FULL_MAX_PAGES 只白耗配额
                            break
                        continue

                    if not raw_items:
                        if phase == "deep":  # 站点封顶已到底：记「已扫到底」，不再每轮从近期页重扫
                            repository.set_coin_filter_scan_page(cate, DEEP_DONE_PAGE)
                        elif not full:  # 增量：没有更多页可挖，本类收工
                            repository.set_coin_filter_scan_page(cursor_key, 0)
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
                    done = 0
                    if rows:
                        done, absent = repository.update_coin_lot_filter_heat(rows)
                        updated += done
                        missing += absent
                    if phase == "deep":
                        # 断点推进：配额用尽时下一轮从这里继续
                        repository.set_coin_filter_scan_page(cate, page + 1)
                    elif not full:
                        # 增量断点：中断后从下一页续扫；昨日缺口归零则本类收工
                        repository.set_coin_filter_scan_page(cursor_key, page + 1)
                        if _yesterday_gap(config).get(cate, 0) == 0:
                            repository.set_coin_filter_scan_page(cursor_key, 0)
                            break
                        # 站点按 update_time 排序，而它不是成交时间：只能看本页有没有
                        # 命中库内拍品，连续若干页全不命中才认为已扫过专场覆盖范围
                        no_gain_pages = 0 if done else no_gain_pages + 1
                        if no_gain_pages >= NO_GAIN_PAGES_TO_STOP:
                            stopped = "incremental_no_gain"
                            break

                    await _pause()
                if stopped == "quota":
                    break
            if stopped == "quota":
                break
    finally:
        await api.__aexit__()

    if stopped is None and used >= DAILY_QUOTA:
        stopped = "quota"  # 分配额度刚好用尽：与「配额耗尽」同口径记录

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

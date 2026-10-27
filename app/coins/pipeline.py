"""铜币管道：采集 → 匹配 → 定价（spec §14.4 / plan §18.5-18.6）。

不依赖 FastAPI、不感知请求上下文：定时任务（`daily_coins`）、管理页手动触发（`app/tasks.py`）与
CLI（`cli coins-pipeline` / `coins-fetch` / `coins-price`）复用同一段逻辑。

**数据源只有一个小程序网关**（`app/coins/gapi.py`，免登录、免浏览器）：
采集（专场 + 拍品）与查价（站内历史成交）都走它 —— 2026-10-12 起不再需要 Playwright。

失败隔离（spec §14.9）：
- 专场列表拉取失败 → 记日志 + 一条 `http_error`，本轮不采新场（库里已有的照常匹配）。
- 单个专场抓失败 → 只记日志，继续下一个专场。
- 查价失败 → 只记日志，该条拍品留「未查价」，不影响其他拍品。
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import repository
from app.coins import categories, coinmap, matcher, pricing
from app.coins import recommend as coins_recommend
from app.coins.gapi import KIND_LIVE, GapiError, MxiqiGapi
from app.config import Config
from app.models import CoinFetchRun, CoinFetchStatus, CoinLot, CoinSession, CoinShop

logger = logging.getLogger(__name__)

RUN_KIND_SESSIONS = "sessions"
RUN_KIND_DETAIL = "session_detail"
RUN_KIND_FINALIZE = "session_finalize"
RUN_KIND_PRICE = "price_search"
RUN_KIND_CATEGORY = "category_backfill"

# 终态回采的起抓缓冲：实时专场存的是开拍时间，等整场结束再取（plan/coins.md §18.18）
FINALIZE_GRACE_HOURS = 6

# 采集日志里的 target：采集走 gapi，写清用的哪个接口
TARGET_LIST = "gapi:" + "miniprogram.auction.ListAuction"


@dataclass(frozen=True)
class CoinsRunResult:
    """一轮铜币管道的汇总，供 CLI / 调度器打印。"""

    status: str
    sessions_seen: int
    sessions_stored: int
    lots_stored: int
    lots_recommended: int
    lots_priced: int
    lots_pricing_skipped: int
    errors: int
    # 因「单轮查价上限」推迟到下一轮的条数（查价要登录，宁可慢也别触发风控）
    lots_pricing_deferred: int = 0
    # 命中里没有「面值/品类」这类具体词 → 关键词太泛，**主动不报价**（spec §14.6 红线）
    lots_pricing_too_generic: int = 0
    # 终态回采（plan/coins.md §18.18）：回采场次数、成交条数、未成交条数
    sessions_finalized: int = 0
    lots_sold: int = 0
    lots_unsold: int = 0
    # 成交库总量（按来源），用于核对 session_final 与拍品终态是否对齐
    sales_session_final: int = 0
    sales_search: int = 0


@dataclass(frozen=True)
class CategoryBackfillResult:
    """历史分类回填的汇总，供 CLI 打印（plan §18.19.5）。"""

    sessions: int
    lots_updated: int
    errors: int


def _record(
    kind: str,
    target: str | None,
    started_at: str,
    status: str,
    *,
    items_new: int = 0,
    error: str | None = None,
    http_status: int | None = None,
) -> None:
    repository.record_coin_fetch_run(
        CoinFetchRun(
            id=None,
            kind=kind,
            target=target,
            started_at=started_at,
            finished_at=repository.utcnow(),
            status=status,
            http_status=http_status,
            items_new=items_new,
            error=error,
        )
    )


async def run(
    config: Config,
    *,
    gapi: MxiqiGapi | None = None,
    collect_only: bool = False,
    price_only: bool = False,
    finalize_only: bool = False,
) -> CoinsRunResult:
    """跑一轮铜币管道。

    - 默认：采集 → 回采终态 → 匹配 → 定价
    - `collect_only=True`：只采集 + 匹配（不回采、不查价）
    - `finalize_only=True`：只回采已结束专场的终态并同步成交库（不采集、不查价）
    - `price_only=True`：只对已入库的推荐拍品查价（不采集、不回采）
    - `gapi`：注入替身（测试用）；不传就自己开一个（整个模块唯一的站点客户端）
    """
    started = repository.utcnow()
    errors = 0
    sessions_seen = sessions_stored = lots_stored = 0

    owned = gapi is None
    api = gapi or MxiqiGapi(config)
    if owned:
        await api.__aenter__()
    try:
        sessions_finalized = lots_sold = lots_unsold = 0
        if finalize_only:
            # 单独回采（运行控制的「回采」按钮 / 定时任务独立开关）
            sessions_finalized, lots_sold, lots_unsold = await _finalize(config, api, started)
            repository.upsert_coin_sales(repository.list_coin_sold_lots())
            sales = repository.count_coin_sales_by_source()
            logger.info(
                "终态回采完成：%d 场（成交 %d / 未成交 %d）；成交库 session_final %d 条",
                sessions_finalized,
                lots_sold,
                lots_unsold,
                sales.get("session_final", 0),
            )
            return CoinsRunResult(
                status=CoinFetchStatus.OK,
                sessions_seen=0,
                sessions_stored=0,
                lots_stored=0,
                lots_recommended=0,
                lots_priced=0,
                lots_pricing_skipped=0,
                errors=0,
                sessions_finalized=sessions_finalized,
                lots_sold=lots_sold,
                lots_unsold=lots_unsold,
                sales_session_final=sales.get("session_final", 0),
                sales_search=sales.get("sold_search", 0),
            )

        if not price_only:
            sessions_seen, sessions_stored, lots_stored = await _collect(config, api, started)
            if not collect_only:
                # 终态回采：已结束的专场每场再取 1 次详情（plan/coins.md §18.18）
                sessions_finalized, lots_sold, lots_unsold = await _finalize(config, api, started)
                # 成交库（高置信来源）：直接消费刚回采到的终态，不额外请求站点
                stored = repository.upsert_coin_sales(repository.list_coin_sold_lots())
                if stored:
                    logger.info("成交库：同步 %d 条终态成交（source=session_final）", stored)

        # 收藏画像优先：配了收藏系列映射就按推荐优先级匹配；否则回退旧的标签匹配
        recommend_result = coins_recommend.rematch_all(config)
        if recommend_result.profiles_total:
            match_result = recommend_result
            price_keywords = recommend_result.keywords
            logger.info(
                "铜币推荐匹配：拍品 %d 条，命中 %d 条（推荐项 %d 个）",
                match_result.lots_total,
                match_result.lots_recommended,
                match_result.items_total,
            )
        else:
            match_result = matcher.rematch_all(config)
            price_keywords = None
            logger.info(
                "铜币匹配（回退画像标签）：拍品 %d 条，命中 %d 条（阈值 %g）",
                match_result.lots_total,
                match_result.lots_recommended,
                match_result.threshold,
            )

        # 币种映射（plan §18.18.5）：独立于 match_score，只写 series/issue/置信度
        mapping = coinmap.remap_all()
        logger.info(
            "拍品币种映射：系列 %d/%d，单版 %d；match_method %s",
            mapping.lots_series_mapped,
            mapping.lots_total,
            mapping.lots_issue_mapped,
            "、".join(f"{key} {count}" for key, count in sorted(mapping.by_method.items())) or "无",
        )

        if collect_only:
            sales = repository.count_coin_sales_by_source()
            return CoinsRunResult(
                CoinFetchStatus.OK,
                sessions_seen,
                sessions_stored,
                lots_stored,
                match_result.lots_recommended,
                0,
                0,
                errors,
                sessions_finalized=sessions_finalized,
                lots_sold=lots_sold,
                lots_unsold=lots_unsold,
                sales_session_final=sales.get("session_final", 0),
                sales_search=sales.get("sold_search", 0),
            )

        priced, skipped, deferred, generic, price_errors = await _price_recommended(
            config, api, keywords=price_keywords
        )
        errors += price_errors
    finally:
        if owned:
            await api.__aexit__(None, None, None)

    sales = repository.count_coin_sales_by_source()
    logger.info(
        "铜币管道结束：专场 %d（入库 %d）拍品 %d 条，推荐 %d 条，"
        "已查价 %d 条，跳过 %d 条，推迟 %d 条，关键词太泛不报价 %d 条，"
        "终态回采 %d 场（成交 %d / 未成交 %d）；成交库 session_final %d 条、sold_search %d 条",
        sessions_seen,
        sessions_stored,
        lots_stored,
        match_result.lots_recommended,
        priced,
        skipped,
        deferred,
        generic,
        sessions_finalized,
        lots_sold,
        lots_unsold,
        sales.get("session_final", 0),
        sales.get("sold_search", 0),
    )
    return CoinsRunResult(
        status=CoinFetchStatus.OK,
        sessions_seen=sessions_seen,
        sessions_stored=sessions_stored,
        lots_stored=lots_stored,
        lots_recommended=match_result.lots_recommended,
        lots_priced=priced,
        lots_pricing_skipped=skipped,
        errors=errors,
        lots_pricing_deferred=deferred,
        lots_pricing_too_generic=generic,
        sessions_finalized=sessions_finalized,
        lots_sold=lots_sold,
        lots_unsold=lots_unsold,
        sales_session_final=sales.get("session_final", 0),
        sales_search=sales.get("sold_search", 0),
    )


async def _collect(config: Config, api: MxiqiGapi, started: str) -> tuple[int, int, int]:
    """采集专场与拍品。返回（看到专场数, 入库专场数, 入库拍品数）。"""
    # 分类字典：每日采集开始前从站点同步一次，接口失败回落种子字典（v23）
    await categories.sync(api)
    try:
        cards = await api.list_sessions()
    except GapiError as exc:
        logger.warning("铜币专场列表拉取失败：%s", exc)
        _record(RUN_KIND_SESSIONS, TARGET_LIST, started, CoinFetchStatus.HTTP_ERROR, error=str(exc))
        return 0, 0, 0

    # 只抓当日场次（spec §14.17）：实时专场看开拍时间（start_time）、限时专场看截拍时间
    # （end_time）是否落在业务时区的「今天」
    day_start, day_end = repository.local_day_bounds(config.app.tz)

    def _day_at(card) -> str | None:
        return card.start_at if card.kind == KIND_LIVE else card.end_at

    before = len(cards)
    cards = [
        card
        for card in cards
        if (at := _day_at(card)) is not None and day_start <= at < day_end
    ]
    logger.info("铜币采集：今日开拍/截拍专场 %d 个（列表共 %d 个）", len(cards), before)

    limit = config.coins.max_sessions_per_run
    cards = cards[:limit]
    logger.info("铜币采集：专场列表 %d 个（上限 %d）", len(cards), limit)

    sessions_stored = 0
    lots_stored = 0
    # 限速：场详情逐场请求，第 2 场起才等（第一场不必白等），带抖动避免节奏固定
    interval = config.coins.request_interval_seconds
    for index, card in enumerate(cards):
        if index > 0 and interval > 0:
            delay = random.uniform(interval * 0.6, interval * 1.4)
            logger.info("采集限速：等 %.1f 秒（每场间隔 %d 秒）", delay, interval)
            await asyncio.sleep(delay)
        detail_started = repository.utcnow()
        try:
            # 详情页拿不到的字段用列表页那张卡兜底（gapi.detail_from_payload）
            detail = await api.session_detail(card.mxiqi_id, fallback=card)
        except GapiError as exc:
            logger.warning("专场 %s 抓取失败，跳过：%s", card.mxiqi_id, exc)
            _record(
                RUN_KIND_DETAIL, card.mxiqi_id, detail_started, CoinFetchStatus.PARSE_ERROR, error=str(exc)
            )
            continue

        shop_id = None
        if detail.card.shop is not None:
            shop = detail.card.shop
            shop_id = repository.upsert_coin_shop(
                CoinShop(
                    id=None,
                    mxiqi_uid=shop.mxiqi_uid or f"shop-{card.mxiqi_id}",
                    name=shop.name,
                    avatar_url=shop.avatar_url,
                    badges=shop.badges,
                    fetched_at=repository.utcnow(),
                )
            )

        session_id = repository.upsert_coin_session(
            CoinSession(
                id=None,
                mxiqi_id=detail.card.mxiqi_id,
                shop_id=shop_id,
                title=detail.card.title,
                status=detail.card.status,
                # 实时专场 = 开拍时间（start_time），限时专场 = 截拍时间（end_time）；
                # 同时存 UTC ISO 与原始文案
                auction_at=detail.end_at,
                kind=detail.card.kind,
                auction_at_raw=detail.card.auction_at_raw,
                deposit=detail.deposit,
                lot_total=detail.card.lot_total,
                view_count=detail.card.view_count,
                cover_urls=detail.card.cover_urls,
                fetched_at=repository.utcnow(),
                commission_rate=detail.commission_rate,
                commission_min=detail.commission_min,
                express_desc=detail.express_desc,
                express_day=detail.express_day,
                express_fee=detail.express_fee,
                express_free_amount=detail.express_free_amount,
                is_storage=detail.is_storage,
            )
        )
        sessions_stored += 1

        # 分类码先确保在字典表里（外键要求；字典外的码以 name=code 兜底）
        categories.ensure_codes(
            lot.category_code for lot in (*card.preview_lots, *detail.lots)
        )

        # 列表卡预览带 bid_num / bid_user_num（详情没有），先写；
        # 同一条拍品的正式字段随后由详情覆盖，热度用 COALESCE 不会被清空
        for lot in card.preview_lots:
            repository.upsert_coin_lot(
                CoinLot(
                    id=None,
                    session_id=session_id,
                    mxiqi_id=f"{detail.card.mxiqi_id}:{lot.mxiqi_id}",
                    lot_no=lot.lot_no,
                    title=lot.title,
                    start_price=lot.start_price,
                    current_price=lot.current_price,
                    image_urls=lot.image_urls,
                    tags=lot.tags,
                    match_score=0.0,
                    is_recommended=False,
                    fetched_at=repository.utcnow(),
                    bid_num=lot.bid_num,
                    bid_user_num=lot.bid_user_num,
                    bid_source=lot.bid_source,
                    image_keys=lot.image_keys,
                    category_code=lot.category_code,
                    is_graded=lot.is_graded,
                )
            )

        new_lots = 0
        for lot in detail.lots:
            repository.upsert_coin_lot(
                CoinLot(
                    id=None,
                    session_id=session_id,
                    # 站内拍品 id 单独用不唯一，用「专场 id + 站内拍品 id」拼稳定去重键
                    mxiqi_id=f"{detail.card.mxiqi_id}:{lot.mxiqi_id}",
                    lot_no=lot.lot_no,
                    title=lot.title,
                    start_price=lot.start_price,
                    current_price=lot.current_price,
                    image_urls=lot.image_urls,
                    tags=lot.tags,
                    match_score=0.0,
                    is_recommended=False,
                    fetched_at=repository.utcnow(),
                    bid_num=lot.bid_num,
                    bid_user_num=lot.bid_user_num,
                    bid_source=lot.bid_source,
                    image_keys=lot.image_keys,
                    category_code=lot.category_code,
                    is_graded=lot.is_graded,
                )
            )
            new_lots += 1
        lots_stored += new_lots
        _record(
            RUN_KIND_DETAIL, detail.card.mxiqi_id, detail_started, CoinFetchStatus.OK, items_new=new_lots
        )
    # 整轮采集的汇总行：放在采集结束时记，别放到查价之后 ——
    # 否则会盖住「查价失败」那条，页面/日志就看不到真实状态了。
    _record(RUN_KIND_SESSIONS, TARGET_LIST, started, CoinFetchStatus.OK, items_new=sessions_stored)
    return len(cards), sessions_stored, lots_stored


async def _finalize(config: Config, api: MxiqiGapi, run_started: str) -> tuple[int, int, int]:
    """回采已结束专场的拍品终态，返回（场次, 成交条数, 未成交条数）。

    每场只发 1 次 `auction.get`（详情一次给全拍品，plan/coins.md §18.18）；
    只对已有终态的拍品写库，仍在拍的（`status=1`）留空，下一轮再取。
    本轮刚采过的场次（`fetched_at >= run_started`）跳过，不重复请求。
    """
    days = config.coins.finalize_days
    if days <= 0:
        return 0, 0, 0
    categories.sync_seed()

    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    before = (now - timedelta(hours=FINALIZE_GRACE_HOURS)).isoformat().replace("+00:00", "Z")
    pending = repository.list_coin_sessions_pending_finalize(
        since, before, run_started, config.coins.max_sessions_per_run
    )
    if not pending:
        return 0, 0, 0
    logger.info("终态回采：待补 %d 个已结束专场（近 %d 天）", len(pending), days)

    interval = config.coins.request_interval_seconds
    sessions_done = sold = unsold = 0
    for index, (session_id, mxiqi_id) in enumerate(pending):
        if index > 0 and interval > 0:
            delay = random.uniform(interval * 0.6, interval * 1.4)
            logger.info("终态回采限速：等 %.1f 秒", delay)
            await asyncio.sleep(delay)

        detail_started = repository.utcnow()
        try:
            detail = await api.session_detail(mxiqi_id)
        except GapiError as exc:
            logger.warning("终态回采失败 %s：%s", mxiqi_id, exc)
            _record(
                RUN_KIND_FINALIZE,
                mxiqi_id,
                detail_started,
                CoinFetchStatus.HTTP_ERROR,
                error=str(exc),
            )
            continue

        batch_sold = batch_unsold = 0
        categories.ensure_codes(lot.category_code for lot in detail.lots)
        for lot in detail.lots:
            if lot.outcome is None:
                continue  # 还在拍，等整场结束下一轮再取
            repository.upsert_coin_lot(
                CoinLot(
                    id=None,
                    session_id=session_id,
                    mxiqi_id=f"{mxiqi_id}:{lot.mxiqi_id}",
                    lot_no=lot.lot_no,
                    title=lot.title,
                    start_price=lot.start_price,
                    current_price=lot.current_price,
                    image_urls=lot.image_urls,
                    tags=lot.tags,
                    match_score=0.0,
                    is_recommended=False,
                    fetched_at=repository.utcnow(),
                    outcome=lot.outcome,
                    final_price=lot.final_price,
                    winner_uid=lot.winner_uid,
                    closed_at=lot.closed_at,
                    bid_num=lot.bid_num,
                    bid_user_num=lot.bid_user_num,
                    bid_source=lot.bid_source,
                    image_keys=lot.image_keys,
                    category_code=lot.category_code,
                    is_graded=lot.is_graded,
                )
            )
            if lot.outcome == "sold":
                batch_sold += 1
            else:
                batch_unsold += 1

        # 拍场状态以站内为准：已截拍的专场同步成 closed，列表页与拍品页才有「已截拍」
        # 围观数（`auction_stat.pv`）是专场级累计值，随回采一并落库
        if detail.card.status is not None:
            repository.set_coin_session_status(
                session_id, detail.card.status, view_count=detail.card.view_count
            )

        sessions_done += 1
        sold += batch_sold
        unsold += batch_unsold
        _record(
            RUN_KIND_FINALIZE,
            mxiqi_id,
            detail_started,
            CoinFetchStatus.OK,
            items_new=batch_sold + batch_unsold,
        )

    logger.info(
        "终态回采完成：%d 场，成交 %d 条，未成交 %d 条", sessions_done, sold, unsold
    )
    return sessions_done, sold, unsold


async def backfill_categories(
    config: Config,
    *,
    days: int | None = None,
    limit: int | None = None,
    gapi: MxiqiGapi | None = None,
) -> CategoryBackfillResult:
    """按专场回采历史拍品的分类码（plan §18.19.5）。

    历史拍品入库时站点还没给分类，只能按专场再取 1 次 `auction.get`
    （详情一次给全拍品，每场只花 1 次请求）；只写 `category_code`，不动其他列，
    已经全覆盖的场次直接跳过（`list_coin_sessions_pending_category` 的 EXISTS 条件）。

    `days`：只看 `fetched_at` 近 N 天的专场；不传则从头补。
    `limit`：单轮最多补多少场；不传则不限。
    """
    categories.sync_seed()

    since = "1970-01-01T00:00:00Z"
    if days is not None:
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    pending = repository.list_coin_sessions_pending_category(since, limit)
    if not pending:
        logger.info("分类回填：没有待补的专场")
        return CategoryBackfillResult(sessions=0, lots_updated=0, errors=0)
    logger.info("分类回填：待补 %d 个专场（fetched_at >= %s）", len(pending), since)

    owned = gapi is None
    api = gapi or MxiqiGapi(config)
    if owned:
        await api.__aenter__()
    sessions_done = lots_updated = errors = 0
    interval = config.coins.request_interval_seconds
    try:
        for index, (_session_id, mxiqi_id) in enumerate(pending):
            if index > 0 and interval > 0:
                delay = random.uniform(interval * 0.6, interval * 1.4)
                logger.info("分类回填限速：等 %.1f 秒", delay)
                await asyncio.sleep(delay)

            started = repository.utcnow()
            try:
                detail = await api.session_detail(mxiqi_id)
            except GapiError as exc:
                logger.warning("分类回填失败 %s：%s", mxiqi_id, exc)
                _record(
                    RUN_KIND_CATEGORY,
                    mxiqi_id,
                    started,
                    CoinFetchStatus.HTTP_ERROR,
                    error=str(exc),
                )
                errors += 1
                continue

            updates: list[tuple[str, str]] = []
            for lot in detail.lots:
                if not lot.category_code:
                    continue
                updates.append((f"{mxiqi_id}:{lot.mxiqi_id}", lot.category_code))
            # 外键要求：分类码先确保在字典表里（字典外的码 name=code 兜底）
            categories.ensure_codes(code for _key, code in updates)
            written = repository.update_coin_lot_categories(updates)
            sessions_done += 1
            lots_updated += written
            _record(
                RUN_KIND_CATEGORY,
                mxiqi_id,
                started,
                CoinFetchStatus.OK,
                items_new=written,
            )
    finally:
        if owned:
            await api.__aexit__(None, None, None)

    logger.info(
        "分类回填完成：%d 场，写入分类 %d 条，失败 %d 场",
        sessions_done,
        lots_updated,
        errors,
    )
    return CategoryBackfillResult(
        sessions=sessions_done, lots_updated=lots_updated, errors=errors
    )


async def _price_recommended(
    config: Config, client: MxiqiGapi, *, keywords: dict[int, str] | None = None
) -> tuple[int, int, int, int, int]:
    """给「推荐且今天还没查过价」的拍品查推荐价。

    返回（已查, 当日跳过, 因上限推迟, 关键词太泛不报价, 失败）。

    `keywords` 非空时走**收藏画像推荐**：直接用推荐项生成的关键词，不再从拍品标题反推标签。
    `keywords` 为 None 时回退旧的标签匹配（与 v1 行为一致）。

    **限速**（用户 2026-10-12 要求：不要求效率）：每次查价之间停 `query_interval_seconds`
    （带 ±40% 抖动，避免固定节奏），并且单轮最多查 `max_queries_per_run` 条 —— 剩下的留到下一轮。
    """
    day_start = repository.local_day_bounds(config.app.tz)[0]
    tags = repository.list_coin_profile_tags(only_enabled=True) if keywords is None else []
    price_cfg = config.coins.price
    base_interval = price_cfg.query_interval_seconds
    cap = price_cfg.max_queries_per_run

    priced = 0
    skipped = 0
    deferred = 0
    generic = 0
    errors = 0
    attempts = 0
    for lot in repository.list_coin_lots_for_matching():
        if not lot.is_recommended or lot.id is None:
            continue
        existing = repository.get_coin_price_ref(lot.id)
        if existing and existing.queried_at and existing.queried_at >= day_start:
            skipped += 1  # 当日缓存，避免重复请求（plan §18.6）
            continue

        if keywords is not None:
            keyword = keywords.get(lot.id)
            if not keyword:
                continue
        else:
            hits = matcher.match_lot(lot.title, tags)
            if not matcher.has_specific_hit(hits):
                # 只有国家/年代这类泛词 → 搜出来的同类币根本不是同一档东西，
                # 报出来的价会离谱（实测：当前价 ¥330 的银套币被报成 ¥2）。宁可空着。
                logger.info(
                    "关键词太泛，不报价 lot_id=%s 命中=%s title=%s",
                    lot.id,
                    [hit.value for hit in hits],
                    lot.title[:40],
                )
                generic += 1
                continue
            keyword = matcher.build_keyword(hits)

        if attempts >= cap:
            deferred += 1  # 本轮不查了，留到下一轮（限速优先）
            continue

        # 限速：第 2 条起才等，第一条不必白等
        if attempts > 0 and base_interval > 0:
            delay = random.uniform(base_interval * 0.6, base_interval * 1.4)
            logger.info(
                "查价限速：等 %.1f 秒（本轮上限 %d 条，已查 %d）", delay, cap, attempts
            )
            await asyncio.sleep(delay)
        attempts += 1

        started = repository.utcnow()
        try:
            ref = await pricing.price_lot(
                client,
                lot.id,
                keyword,
                statistic=price_cfg.statistic,
                min_samples=price_cfg.min_samples,
                window_days=price_cfg.window_days,
            )
        except GapiError as exc:
            logger.warning("查价失败 lot_id=%s：%s", lot.id, exc)
            _record(RUN_KIND_PRICE, keyword, started, CoinFetchStatus.HTTP_ERROR, error=str(exc))
            errors += 1
            continue

        repository.upsert_coin_price_ref(ref)
        # 成交搜索命中的条目同时进成交库（中置信，不覆盖 session_final）
        repository.upsert_coin_sales(pricing.sales_from_ref(ref))
        # home.filter 带热度，按拍品 id 回填（NULL 不覆盖已有值）
        repository.update_coin_lot_bids(pricing.bid_updates_from_ref(ref))
        _record(RUN_KIND_PRICE, keyword, started, CoinFetchStatus.OK, items_new=ref.sample_count)
        priced += 1

    if deferred:
        logger.info("本轮查价上限 %d 条已到，%d 条推迟到下一轮", cap, deferred)
    if generic:
        logger.info("%d 条命中里没有面值/品类，关键词太泛，主动不报价", generic)
    return priced, skipped, deferred, generic, errors

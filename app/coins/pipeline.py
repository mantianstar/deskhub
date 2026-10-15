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

from app import repository
from app.coins import matcher, pricing
from app.coins.gapi import GapiError, MxiqiGapi
from app.config import Config
from app.models import CoinFetchRun, CoinFetchStatus, CoinLot, CoinSession, CoinShop

logger = logging.getLogger(__name__)

RUN_KIND_SESSIONS = "sessions"
RUN_KIND_DETAIL = "session_detail"
RUN_KIND_PRICE = "price_search"

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
) -> CoinsRunResult:
    """跑一轮铜币管道。

    - 默认：采集 → 匹配 → 定价
    - `collect_only=True`：只采集 + 匹配（不查价）
    - `price_only=True`：只对已入库的推荐拍品查价（不采集）
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
        if not price_only:
            sessions_seen, sessions_stored, lots_stored = await _collect(config, api, started)

        match_result = matcher.rematch_all(config)
        logger.info(
            "铜币匹配：拍品 %d 条，命中 %d 条（阈值 %g）",
            match_result.lots_total,
            match_result.lots_recommended,
            match_result.threshold,
        )

        if collect_only:
            return CoinsRunResult(
                CoinFetchStatus.OK,
                sessions_seen,
                sessions_stored,
                lots_stored,
                match_result.lots_recommended,
                0,
                0,
                errors,
            )

        priced, skipped, deferred, generic, price_errors = await _price_recommended(config, api)
        errors += price_errors
    finally:
        if owned:
            await api.__aexit__(None, None, None)

    logger.info(
        "铜币管道结束：专场 %d（入库 %d）拍品 %d 条，推荐 %d 条，"
        "已查价 %d 条，跳过 %d 条，推迟 %d 条，关键词太泛不报价 %d 条",
        sessions_seen,
        sessions_stored,
        lots_stored,
        match_result.lots_recommended,
        priced,
        skipped,
        deferred,
        generic,
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
    )


async def _collect(config: Config, api: MxiqiGapi, started: str) -> tuple[int, int, int]:
    """采集专场与拍品。返回（看到专场数, 入库专场数, 入库拍品数）。"""
    try:
        cards = await api.list_sessions()
    except GapiError as exc:
        logger.warning("铜币专场列表拉取失败：%s", exc)
        _record(RUN_KIND_SESSIONS, TARGET_LIST, started, CoinFetchStatus.HTTP_ERROR, error=str(exc))
        return 0, 0, 0

    limit = config.coins.max_sessions_per_run
    cards = cards[:limit]
    logger.info("铜币采集：专场列表 %d 个（上限 %d）", len(cards), limit)

    sessions_stored = 0
    lots_stored = 0
    for card in cards:
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
                # 「拍卖时间」按站内口径 = **截拍时间**（end_time）；同时存 UTC ISO 与原始文案
                auction_at=detail.end_at,
                auction_at_raw=detail.card.auction_at_raw,
                deposit=detail.deposit,
                lot_total=detail.card.lot_total,
                view_count=detail.card.view_count,
                cover_urls=detail.card.cover_urls,
                fetched_at=repository.utcnow(),
            )
        )
        sessions_stored += 1

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


async def _price_recommended(
    config: Config, client: MxiqiGapi
) -> tuple[int, int, int, int, int]:
    """给「推荐且今天还没查过价」的拍品查推荐价。

    返回（已查, 当日跳过, 因上限推迟, 关键词太泛不报价, 失败）。

    **限速**（用户 2026-10-12 要求：不要求效率）：每次查价之间停 `query_interval_seconds`
    （带 ±40% 抖动，避免固定节奏），并且单轮最多查 `max_queries_per_run` 条 —— 剩下的留到下一轮。
    """
    day_start = repository.local_day_bounds(config.app.tz)[0]
    tags = repository.list_coin_profile_tags(only_enabled=True)
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

        keyword = matcher.build_keyword(hits)
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
        _record(RUN_KIND_PRICE, keyword, started, CoinFetchStatus.OK, items_new=ref.sample_count)
        priced += 1

    if deferred:
        logger.info("本轮查价上限 %d 条已到，%d 条推迟到下一轮", cap, deferred)
    if generic:
        logger.info("%d 条命中里没有面值/品类，关键词太泛，主动不报价", generic)
    return priced, skipped, deferred, generic, errors

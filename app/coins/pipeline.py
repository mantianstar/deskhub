"""铜币管道：采集 → 匹配 → 定价（spec §14.4 / plan §18.5-18.6）。

不依赖 FastAPI、不感知请求上下文：调度器（`startup_coins` / `daily_coins`）与
CLI（`cli coins-pipeline` / `coins-fetch` / `coins-price`）复用同一段逻辑。

失败隔离（spec §14.9）：
- 会话被反爬拦下 → 整轮记 `blocked` 并立即返回（不做无意义的继续请求）。
- 单个专场抓失败 → 只记日志，继续下一个专场。
- 定价失败 → 只记日志，该条拍品留「未查价」，不影响其他拍品。
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

from app import repository
from app.coins import matcher, pricing
from app.coins.mxiqi import BlockedError, MxiqiClient, MxiqiError
from app.config import Config
from app.models import CoinFetchRun, CoinFetchStatus, CoinLot, CoinSession, CoinShop

logger = logging.getLogger(__name__)

RUN_KIND_SESSIONS = "sessions"
RUN_KIND_DETAIL = "session_detail"
RUN_KIND_PRICE = "price_search"


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
    client: MxiqiClient | None = None,
    cdp_url: str | None = None,
    collect_only: bool = False,
    price_only: bool = False,
) -> CoinsRunResult:
    """跑一轮铜币管道。

    - 默认：采集 → 匹配 → 定价
    - `collect_only=True`：只采集 + 匹配（不查价）
    - `price_only=True`：只对已入库的推荐拍品查价（不采集）
    - `cdp_url`：走 CDP 连本机已打开的 Chrome（人工过滑块最稳）；传了 `client` 时忽略
    """
    owned = client is None
    active = client or MxiqiClient(config, cdp_url=cdp_url)
    started = repository.utcnow()
    errors = 0
    sessions_seen = sessions_stored = lots_stored = 0

    try:
        if owned:
            await active.__aenter__()

        if not price_only:
            sessions_seen, sessions_stored, lots_stored, blocked = await _collect(config, active, started)
            if blocked is not None:
                return blocked

        match_result = matcher.rematch_all(config)
        logger.info(
            "铜币匹配：拍品 %d 条，命中 %d 条（阈值 %g）",
            match_result.lots_total,
            match_result.lots_recommended,
            match_result.threshold,
        )

        if collect_only:
            _record(
                RUN_KIND_SESSIONS, "/auction", started, CoinFetchStatus.OK, items_new=sessions_stored
            )
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

        priced, skipped, deferred, generic, price_errors = await _price_recommended(config, active)
        errors += price_errors

        _record(RUN_KIND_SESSIONS, "/auction", started, CoinFetchStatus.OK, items_new=sessions_stored)
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
    finally:
        if owned:
            await active.__aexit__(None, None, None)


async def _collect(
    config: Config, client: MxiqiClient, started: str
) -> tuple[int, int, int, CoinsRunResult | None]:
    """采集专场与拍品。返回（看到专场数, 入库专场数, 入库拍品数, 需要提前返回的 blocked 结果）。"""
    try:
        cards = await client.list_sessions()
    except BlockedError as exc:
        logger.warning("铜币采集被反爬拦下：%s", exc)
        _record(RUN_KIND_SESSIONS, "/auction", started, CoinFetchStatus.BLOCKED, error=str(exc))
        return 0, 0, 0, CoinsRunResult(CoinFetchStatus.BLOCKED, 0, 0, 0, 0, 0, 0, 1)
    except MxiqiError as exc:
        _record(RUN_KIND_SESSIONS, "/auction", started, CoinFetchStatus.PARSE_ERROR, error=str(exc))
        return 0, 0, 0, CoinsRunResult(CoinFetchStatus.PARSE_ERROR, 0, 0, 0, 0, 0, 0, 1)

    limit = config.coins.max_sessions_per_run
    cards = cards[:limit]
    logger.info("铜币采集：专场列表 %d 个（上限 %d）", len(cards), limit)

    sessions_stored = 0
    lots_stored = 0
    for card in cards:
        detail_started = repository.utcnow()
        try:
            # 详情页拿不到围观数/封面图，用列表页那张卡兜底（mxiqi.parse_session_detail）
            detail = await client.session_detail(card.mxiqi_id, fallback=card)
        except BlockedError as exc:
            logger.warning("专场详情被反爬拦下，整轮中止：%s", exc)
            _record(
                RUN_KIND_DETAIL, card.mxiqi_id, detail_started, CoinFetchStatus.BLOCKED, error=str(exc)
            )
            return len(cards), sessions_stored, lots_stored, CoinsRunResult(
                CoinFetchStatus.BLOCKED, len(cards), sessions_stored, lots_stored, 0, 0, 0, 1
            )
        except MxiqiError as exc:
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
                auction_at=None,  # 站内只给「M月D日 星期X HH:MM」文案，原样存 raw
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
                    # 站内没暴露拍品 id，用「专场 id + 站内拍品 id」拼一个稳定去重键
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
    return len(cards), sessions_stored, lots_stored, None


async def _price_recommended(
    config: Config, client: pricing.PriceSource
) -> tuple[int, int, int, int, int]:
    """给「推荐且今天还没查过价」的拍品查推荐价。

    返回（已查, 当日跳过, 因上限推迟, 关键词太泛不报价, 失败）。

    **限速**（用户 2026-10-12 要求：不要求效率，防封号）：成交价接口要登录，
    等于拿本人账号在查，所以每次查价之间停 `query_interval_seconds`（带 ±40% 抖动，
    避免固定节奏），并且单轮最多查 `max_queries_per_run` 条 —— 剩下的留到下一轮。
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
        except BlockedError as exc:
            logger.warning("查价被反爬拦下，停止本轮定价：%s", exc)
            _record(RUN_KIND_PRICE, keyword, started, CoinFetchStatus.BLOCKED, error=str(exc))
            errors += 1
            break
        except MxiqiError as exc:
            logger.warning("查价失败 lot_id=%s：%s", lot.id, exc)
            _record(RUN_KIND_PRICE, keyword, started, CoinFetchStatus.PARSE_ERROR, error=str(exc))
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

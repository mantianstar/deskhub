"""铜币拍卖页面（spec §14.7）。

只做「参数校验 + 调 repository + 渲染模板」——采集与匹配在 `app/coins/*`，
本文件不写 SQL、不发网络请求（同 plan §3.2 的分层约定）。
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime
from math import ceil
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.responses import Response

from app import config, repository, textmatch
from app.coins import analysis as coins_analysis
from app.coins import best as coins_best
from app.coins import collection as coins_collection
from app.coins import explore as coins_explore
from app.coins import explore_page as coins_explore_page
from app.coins import grades as coins_grades
from app.coins import ingest as coins_ingest
from app.coins import numista as coins_numista
from app.coins import numista_web as coins_numista_web
from app.coins import recommend as coins_recommend
from app.coins import search as coins_search
from app.coins import shops as coins_shops
from app.coins.numista import ISSUER_CODES, NumistaError
from app.coins.numista_web import NumistaPageError
from app.models import (
    COIN_EXPLORE_STATUS_LABELS,
    COIN_EXPLORE_STATUSES,
    COIN_PURCHASE_GRADES,
    COIN_SESSION_STATUS_LABELS,
    CoinCollectionSeries,
    CoinSeries,
)
from app.routers import templates

logger = logging.getLogger(__name__)

router = APIRouter()

# 状态的中文名放在 models 里（CLI 也要用，避免 CLI 反过来 import router）
SESSION_STATUS_LABELS = COIN_SESSION_STATUS_LABELS


def format_price(value: float | None) -> str:
    """价格显示：整数不带小数点，缺失显示 `—`。"""
    if value is None:
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


templates.env.filters["price"] = format_price
templates.env.globals["coin_session_status_labels"] = SESSION_STATUS_LABELS


# ---------------------------------------------------------------- 第一页：专场列表


def _recommendation_sessions(
    items: list,
) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    """推荐项 → 各专场的精准 / 相似匹配拍品 id（精准优先，相似不重复计数）。"""
    precise: dict[int, set[int]] = {}
    similar: dict[int, set[int]] = {}
    for item in items:
        bucket = precise if item.priority in (1, 2) else similar
        for lot in item.matched_lots:
            if lot.id is None:
                continue
            bucket.setdefault(lot.session_id, set()).add(lot.id)
    for session_id, lot_ids in similar.items():
        precise_ids = precise.get(session_id)
        if precise_ids:
            lot_ids -= precise_ids
    return precise, similar


@router.get("/coins")
def sessions_page(
    request: Request,
    auction_date: str = "",
    rec: str = "yes",
    match_type: str = Query("all", alias="type"),
    follow: str = "all",
    kind: str = "all",
    page: str = "",
) -> Response:
    """专场列表：按拍卖日期 + 推荐状态 / 推荐类型 / 关注状态 / 拍场类型筛选。

    排序：精准匹配 > 相似推荐 > 已关注店铺 > 普通拍场，同层按截拍时间（库里已是倒序）。
    """
    cfg = config.get()
    try:
        selected_day = datetime.strptime(auction_date, "%Y-%m-%d").date()
    except ValueError:
        selected_day = datetime.now(cfg.app.tz).date()
    rec = rec if rec in ("all", "yes", "no") else "yes"
    match_type = match_type if match_type in ("all", "precise", "similar") else "all"
    follow = follow if follow in ("all", "followed", "unfollowed") else "all"
    kind = kind if kind in ("all", "live", "timed") else "all"
    start, end = repository.day_bounds_local(cfg.app.tz, selected_day)
    cards = repository.list_coin_session_cards(
        day_bounds=(start, end),
        only_recommended=False,
        kind=None if kind == "all" else kind,
    )
    _, items = coins_recommend.load_recommendations(cfg, day=selected_day)
    precise_by_session, similar_by_session = _recommendation_sessions(items)
    if (
        selected_day == datetime.now(cfg.app.tz).date()
        and not precise_by_session
        and not similar_by_session
    ):
        # 命中标记可能停在上一轮采集：打开拍场页时按当前画像重算当日匹配
        coins_recommend.rematch_today(cfg)
        cards = repository.list_coin_session_cards(
            day_bounds=(start, end),
            only_recommended=False,
            kind=None if kind == "all" else kind,
        )
        _, items = coins_recommend.load_recommendations(cfg, day=selected_day)
        precise_by_session, similar_by_session = _recommendation_sessions(items)

    has_items = bool(items)
    for card in cards:
        if has_items:
            card.precise_count = len(precise_by_session.get(card.session_id, set()))
            card.similar_count = len(similar_by_session.get(card.session_id, set()))
        else:
            # 未配置收藏画像时沿用入库的标签匹配结果
            card.precise_count = card.recommended_count
            card.similar_count = 0
    if rec == "yes":
        cards = [card for card in cards if card.precise_count or card.similar_count]
    elif rec == "no":
        cards = [card for card in cards if not (card.precise_count or card.similar_count)]
    if match_type == "precise":
        cards = [card for card in cards if card.precise_count]
    elif match_type == "similar":
        cards = [card for card in cards if card.similar_count]
    if follow == "followed":
        cards = [card for card in cards if card.shop_followed]
    elif follow == "unfollowed":
        cards = [card for card in cards if not card.shop_followed]
    # 稳定排序：同层保留库里的截拍时间倒序
    cards = sorted(
        cards,
        key=lambda card: (
            0
            if card.precise_count
            else 1
            if card.similar_count
            else 2
            if card.shop_followed
            else 3
        ),
    )

    card_total = len(cards)
    paged_cards, page_no, total_pages = _paginate(
        cards, page, COIN_SESSION_PAGE_SIZE
    )
    pager = {
        "page": page_no,
        "total_pages": total_pages,
        "prev_url": _query_url(
            "/coins",
            {
                "auction_date": selected_day.isoformat(),
                "rec": rec,
                "type": match_type,
                "follow": follow,
                "kind": kind,
                "page": page_no - 1,
            },
        )
        if page_no > 1
        else "",
        "next_url": _query_url(
            "/coins",
            {
                "auction_date": selected_day.isoformat(),
                "rec": rec,
                "type": match_type,
                "follow": follow,
                "kind": kind,
                "page": page_no + 1,
            },
        )
        if page_no < total_pages
        else "",
    }
    return templates.TemplateResponse(
        request,
        "coins/sessions.html",
        {
            "request": request,
            "page_title": "拍场",
            "cards": paged_cards,
            "card_total": card_total,
            "pager": pager,
            "auction_date": selected_day.isoformat(),
            "filter_rec": rec,
            "filter_match_type": match_type,
            "filter_follow": follow,
            "filter_kind": kind,
            "summary": repository.coin_fetch_summary(),
        },
    )


# ---------------------------------------------------------------- 全局拍品搜索


def _price_bound(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@router.get("/coins/search")
def search_page(
    request: Request,
    q: str = "",
    price_min: str = "",
    price_max: str = "",
    page: str = "",
) -> Response:
    """全局拍品搜索：关键词（名称 / 年份 / 国家 / 材质）+ 价格区间。

    默认只返回进行中 / 即将开始（running / preview）的拍场拍品。
    """
    cfg = config.get()
    keyword = q.strip()
    lots = coins_search.search_lots(
        cfg,
        keyword=keyword,
        price_min=_price_bound(price_min),
        price_max=_price_bound(price_max),
    )
    lots_total = len(lots)
    paged_lots, page_no, total_pages = _paginate(lots, page, COIN_SEARCH_PAGE_SIZE)
    keep = {"q": keyword, "price_min": price_min, "price_max": price_max}
    pager = {
        "page": page_no,
        "total_pages": total_pages,
        "prev_url": _query_url("/coins/search", {**keep, "page": page_no - 1})
        if page_no > 1
        else "",
        "next_url": _query_url("/coins/search", {**keep, "page": page_no + 1})
        if page_no < total_pages
        else "",
    }
    return templates.TemplateResponse(
        request,
        "coins/search.html",
        {
            "request": request,
            "page_title": "拍品搜索",
            "keyword": keyword,
            "price_min": price_min,
            "price_max": price_max,
            "lots": paged_lots,
            "lots_total": lots_total,
            "pager": pager,
        },
    )


# ---------------------------------------------------------------- 第二页：拍品页


@router.get("/coins/session/{session_id}")
def session_page(request: Request, session_id: int, page: str = "", lot: str = "") -> Response:
    detail = repository.get_coin_session_detail(session_id)
    if detail is None:
        return templates.TemplateResponse(
            request,
            "coins/session.html",
            {
                "request": request,
                "page_title": "专场不存在",
                "detail": None,
            },
            status_code=404,
        )
    precise_reasons, similar_reasons = _lot_recommendations(detail)
    precise_lots = [
        (lot, precise_reasons[lot.lot_id])
        for lot in detail.lots
        if lot.lot_id in precise_reasons
    ]
    similar_lots = [
        (lot, similar_reasons[lot.lot_id])
        for lot in detail.lots
        if lot.lot_id in similar_reasons
    ]
    other_lots = [
        lot
        for lot in detail.lots
        if lot.lot_id not in precise_reasons and lot.lot_id not in similar_reasons
    ]
    other_total = len(other_lots)
    # 从全局搜索跳转时带 lot 参数：目标拍品在其他拍品里则翻到它所在页，锚点才能命中
    if lot:
        for index, item in enumerate(other_lots):
            if str(item.lot_id) == lot:
                page = str(index // COIN_OTHER_LOT_PAGE_SIZE + 1)
                break
    paged_other, other_page, other_pages = _paginate(
        other_lots, page, COIN_OTHER_LOT_PAGE_SIZE
    )
    other_pager = {
        "page": other_page,
        "total_pages": other_pages,
        "prev_url": _query_url(
            f"/coins/session/{session_id}", {"page": other_page - 1, "hide_rec": 1}
        )
        if other_page > 1
        else "",
        "next_url": _query_url(
            f"/coins/session/{session_id}", {"page": other_page + 1, "hide_rec": 1}
        )
        if other_page < other_pages
        else "",
    }
    return templates.TemplateResponse(
        request,
        "coins/session.html",
        {
            "request": request,
            "page_title": detail.title,
            "detail": detail,
            "precise_lots": precise_lots,
            "similar_lots": similar_lots,
            "other_lots": paged_other,
            "other_total": other_total,
            "other_pager": other_pager,
        },
    )


def _lot_recommendations(detail) -> tuple[dict[int, str], dict[int, str]]:
    """拍品 → 精准 / 相似推荐的推荐原因；精准优先，相似不重复计数。"""
    cfg = config.get()
    moment = repository.parse_dt(detail.auction_at)
    day = moment.date() if moment is not None else None
    _, items = coins_recommend.load_recommendations(cfg, day=day)
    if not items:
        # 未配置收藏画像时沿用入库的标签匹配结果，全部视为精准推荐
        return {lot.lot_id: "" for lot in detail.lots if lot.is_recommended}, {}

    precise: dict[int, tuple[tuple[int, float], str]] = {}
    similar: dict[int, tuple[tuple[int, float], str]] = {}
    for item in items:
        bucket = precise if item.priority in (1, 2) else similar
        rank = (item.priority, -item.score)
        for lot in detail.lots:
            if not coins_recommend.lot_matches(item, lot):
                continue
            current = bucket.get(lot.lot_id)
            if current is None or rank < current[0]:
                bucket[lot.lot_id] = (rank, item.reason)
    for lot_id in precise:
        similar.pop(lot_id, None)
    return (
        {lot_id: value[1] for lot_id, value in precise.items()},
        {lot_id: value[1] for lot_id, value in similar.items()},
    )


def _redirect(path: str) -> Response:
    return Response(status_code=302, headers={"Location": path})


def _return_target(raw: str, fallback: str) -> str:
    """只接受铜币模块内的回跳地址，其余回落到默认页。"""
    return raw if raw.startswith("/coins/") else fallback


@router.post("/coins/shop/{shop_id}/follow")
def follow_coin_shop(
    shop_id: int, follow: str = Form("1"), next: str = Form("")
) -> Response:
    """关注 / 取消关注店铺（本地标记）。"""
    repository.set_coin_shop_followed(shop_id, follow == "1")
    return _redirect(_return_target(next, "/coins"))


# ---------------------------------------------------------------- 参考库：查询 / 详情 / 采集管理
# 定义见 spec §14.14 / plan §18.14。页面只读中文表；采集复用 `app.coins.numista` 的编排函数
# （与 CLI 同一份），本文件不写 SQL。


def _parse_year(year: str) -> tuple[int | None, str]:
    """年份输入 → `(int | None, 错误文案)`；空串视为「不限」，非整数回一条提示。"""
    text = (year or "").strip()
    if not text:
        return None, ""
    if not text.lstrip("-").isdigit():
        return None, f"年份「{text}」不是整数，已忽略该条件"
    return int(text), ""


def _parse_size(size: str) -> tuple[float | None, float | None, str]:
    """尺寸输入 → `(下限, 上限, 错误文案)`；空串视为「不限」，支持 `28-33` 或单个数值（不带单位）。"""
    text = (size or "").strip().replace("－", "-").replace("–", "-")
    if not text:
        return None, None, ""
    parts = [part.strip() for part in text.split("-")]
    if len(parts) > 2 or any(not part for part in parts):
        return None, None, f"尺寸「{text}」格式不对，示例 28-33"
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return None, None, f"尺寸「{text}」不是数字，只填数字（不带单位），已忽略该条件"
    return min(values), max(values), ""


def _manage_url(country: str, year: str, denomination: str, composition: str) -> str:
    query = urlencode(
        {
            "country": country,
            "year": year,
            "denomination": denomination,
            "composition": composition,
        }
    )
    return f"/coins/manage?{query}"


def _render_manage(
    request: Request,
    country: str,
    year: str,
    denomination: str,
    composition: str,
    error: str,
) -> Response:
    """渲染管理页（成功重定向前 / 采集失败时就地提示用同一份上下文）。"""
    parsed_year, _ = _parse_year(year)
    series_list = repository.search_coin_series(
        issuer=country or None,
        year=parsed_year,
        denomination=denomination or None,
        composition=composition or None,
        cn=True,
    )
    return templates.TemplateResponse(
        request,
        "coins/manage.html",
        {
            "request": request,
            "page_title": "采集中心",
            "countries": sorted(ISSUER_CODES),
            "country": country,
            "year": year,
            "denomination": denomination,
            "composition": composition,
            "denominations": repository.list_coin_series_denominations(cn=True),
            "compositions": repository.list_coin_series_compositions(cn=True),
            "error": error,
            "series_list": series_list,
            "issue_stats": repository.coin_series_issue_stats(cn=True),
            # 每行的采集状态：年份信息 / 估值 / 图片，逐系列判（图同 spec §14.15：只认本地缓存）
            "value_counts": repository.coin_series_value_counts(),
            "series_images": {
                item.numista_id: {
                    side: coins_numista_web.cached_image(item.numista_id, side) is not None
                    for side in coins_numista_web.IMAGE_SIDES
                }
                for item in series_list
            },
        },
    )


@router.get("/coins/info")
def info_page(
    request: Request,
    object_type: str = "",
    country: str = "",
    year: str = "",
    denomination: str = "",
    composition: str = "",
    size: str = "",
    status: str = "",
    page: str = "",
) -> Response:
    """信息查询页：按货币类型 + 国家 + 年份 + 面值 + 材质 + 尺寸 + 状态查**本地已采集**的系列（不调接口）。"""
    parsed_year, year_error = _parse_year(year)
    size_min, size_max, size_error = _parse_size(size)
    series_list = repository.search_coin_series(
        issuer=country or None,
        year=parsed_year,
        denomination=denomination or None,
        composition=composition or None,
        object_type=object_type or None,
        size_min=size_min,
        size_max=size_max,
        cn=True,
    )
    # 状态筛选与卡片角标口径一致：已收藏 > 已观察 > 未观察（互斥）。
    watched_ids = {item.numista_id for item in repository.list_coin_watchlist()}
    collected_ids = {
        member.numista_series_id for member in repository.list_coin_collection_members()
    }
    if status == "collected":
        series_list = [
            item for item in series_list if item.numista_id in collected_ids
        ]
    elif status == "watched":
        series_list = [
            item
            for item in series_list
            if item.numista_id in watched_ids and item.numista_id not in collected_ids
        ]
    elif status == "unwatched":
        series_list = [
            item
            for item in series_list
            if item.numista_id not in watched_ids and item.numista_id not in collected_ids
        ]
    paged_series, page_no, total_pages = _paginate(
        series_list, page, COIN_LIBRARY_PAGE_SIZE
    )
    filter_params = {
        "object_type": object_type,
        "country": country,
        "year": year,
        "denomination": denomination,
        "composition": composition,
        "size": size,
        "status": status,
    }
    prev_url = (
        _query_url("/coins/info", {**filter_params, "page": page_no - 1})
        if page_no > 1
        else ""
    )
    next_url = (
        _query_url("/coins/info", {**filter_params, "page": page_no + 1})
        if page_no < total_pages
        else ""
    )
    return templates.TemplateResponse(
        request,
        "coins/info.html",
        {
            "request": request,
            "page_title": "币库",
            "object_types": repository.list_coin_series_object_types(cn=True),
            "object_type": object_type,
            "countries": repository.list_coin_series_countries(cn=True),
            "country": country,
            "year": year,
            "denomination": denomination,
            "composition": composition,
            "size": size,
            "denominations": repository.list_coin_series_denominations(cn=True),
            "compositions": repository.list_coin_series_compositions(cn=True),
            "year_error": year_error,
            "size_error": size_error,
            "series_list": paged_series,
            "series_total": len(series_list),
            "commemorative_ids": {
                item.numista_id
                for item in paged_series
                if coins_collection.is_commemorative_object(item.object_type)
            },
            # 图片与详情页同规矩（spec §14.15）：Numista 图不允许跨站引用，
            # 列表卡里只渲染**已缓存到本地**的那一侧，其余给占位（卡片本身是 <a>，不能再嵌链接）。
            "series_images": {
                item.numista_id: {
                    side: coins_numista_web.cached_image(item.numista_id, side) is not None
                    for side in coins_numista_web.IMAGE_SIDES
                }
                for item in paged_series
            },
            "total_series": repository.count_coin_series(cn=True),
            "status": status,
            "watched_ids": watched_ids,
            "collected_ids": collected_ids,
            "year_counts": repository.coin_series_issue_stats(cn=True),
            "page": page_no,
            "total_pages": total_pages,
            "prev_url": prev_url,
            "next_url": next_url,
            "current_url": (
                f"/coins/info?{request.url.query}"
                if request.url.query
                else "/coins/info"
            ),
        },
    )


@router.post("/coins/watchlist/add")
def add_watch_item(
    numista_id: int = Form(...), next: str = Form("")
) -> Response:
    """加入观察系列（幂等；粒度 = 单个 Numista 系列）。"""
    repository.add_coin_watch_item(numista_id)
    return _redirect(_return_target(next, f"/coins/series/{numista_id}"))


@router.get("/coins/watchlist")
def watchlist_page(
    request: Request,
    object_type: str = "",
    country: str = "",
    year: str = "",
    denomination: str = "",
    composition: str = "",
    page: str = "",
) -> Response:
    """观察系列页：已观察系列单列卡片 + 五条件查询（只读本地，不调接口）。"""
    parsed_year, year_error = _parse_year(year)
    watched = repository.list_coin_watchlist()
    watched_ids = [item.numista_id for item in watched]
    series_list = []
    if watched_ids:
        found = {
            item.numista_id: item
            for item in repository.search_coin_series(
                issuer=country or None,
                year=parsed_year,
                denomination=denomination or None,
                composition=composition or None,
                object_type=object_type or None,
                numista_ids=watched_ids,
                cn=True,
            )
        }
        # 按观察时间倒序（新加入在前），而不是系列表的 issuer 顺序
        series_list = [found[numista_id] for numista_id in watched_ids if numista_id in found]
    paged_series, page_no, total_pages = _paginate(
        series_list, page, COIN_WATCHLIST_PAGE_SIZE
    )
    filter_params = {
        "object_type": object_type,
        "country": country,
        "year": year,
        "denomination": denomination,
        "composition": composition,
    }
    prev_url = (
        _query_url("/coins/watchlist", {**filter_params, "page": page_no - 1})
        if page_no > 1
        else ""
    )
    next_url = (
        _query_url("/coins/watchlist", {**filter_params, "page": page_no + 1})
        if page_no < total_pages
        else ""
    )
    return templates.TemplateResponse(
        request,
        "coins/watchlist.html",
        {
            "request": request,
            "page_title": "观察系列",
            "object_types": repository.list_coin_series_object_types(cn=True),
            "object_type": object_type,
            "countries": repository.list_coin_series_countries(cn=True),
            "country": country,
            "year": year,
            "denomination": denomination,
            "composition": composition,
            "denominations": repository.list_coin_series_denominations(cn=True),
            "compositions": repository.list_coin_series_compositions(cn=True),
            "year_error": year_error,
            "series_list": paged_series,
            "series_total": len(series_list),
            "series_images": {
                item.numista_id: {
                    side: coins_numista_web.cached_image(item.numista_id, side) is not None
                    for side in coins_numista_web.IMAGE_SIDES
                }
                for item in paged_series
            },
            "watched_total": len(watched),
            "year_counts": repository.coin_series_issue_stats(cn=True),
            "collections": repository.list_coin_collection_series(),
            "page": page_no,
            "total_pages": total_pages,
            "prev_url": prev_url,
            "next_url": next_url,
            "current_url": (
                f"/coins/watchlist?{request.url.query}"
                if request.url.query
                else "/coins/watchlist"
            ),
        },
    )


@router.post("/coins/watchlist/remove")
def remove_watch_item(
    numista_id: int = Form(...), next: str = Form("")
) -> Response:
    """放弃观察（幂等；粒度 = 单个 Numista 系列）。"""
    repository.remove_coin_watch_item(numista_id)
    return _redirect(_return_target(next, "/coins/watchlist"))


@router.post("/coins/watchlist/upgrade")
def upgrade_watch_items(
    numista_ids: list[int] = Form(...),
    dest: str = Form("existing"),
    collection_id: int = Form(0),
    name: str = Form(""),
    country: str = Form(""),
    ruler: str = Form(""),
    denomination: str = Form(""),
) -> Response:
    """观察系列 → 收藏系列：默认加入已有收藏系列，也可按弹窗表单新建。"""
    target_id = collection_id
    if dest == "new":
        country = country.strip()
        denomination = denomination.strip()
        if not country or not denomination:
            return Response(status_code=400)
        ruler_value = ruler.strip() or None
        target_id = repository.upsert_coin_collection_series(
            collection_key=f"{country}-{ruler_value or '-'}-{denomination}",
            name=name.strip()
            or " · ".join(
                part
                for part in (country, ruler_value or "-", denomination)
                if part
            ),
            country=country,
            ruler=ruler_value,
            denomination=denomination,
        )
    if repository.get_coin_collection_series(target_id) is None:
        return Response(status_code=400)
    for numista_id in dict.fromkeys(numista_ids):
        series = repository.get_coin_series(numista_id, cn=True)
        if series is None:
            continue
        member_name = " · ".join(
            part for part in (series.issuer, series.ruler, series.denomination) if part
        ) or series.title
        repository.upsert_coin_collection_member(target_id, numista_id, member_name)
        repository.remove_coin_watch_item(numista_id)
    return _redirect(f"/coins/collections/{target_id}")


@router.get("/coins/series/{numista_id}")
def series_page(
    request: Request,
    numista_id: int,
    issue: int | None = Query(None),
    sort: str = Query("sold"),
    order: str = Query("desc"),
    page: int = Query(1),
) -> Response:
    """系列详情：系列信息 + 各年份信息表 + 各年份估值表（估值只读展示）。

    尚未采纳的候选（只存在于候选池）也走这里：用候选保存的详情渲染，避免跳去 Numista 站点。
    「麦稀奇成交」的筛选 / 排序 / 分页都走服务端 query 参数；`issue` 单选，
    每次只保留一个单版，点另一个价格即替换。
    """
    series = repository.get_coin_series(numista_id, cn=True)
    is_candidate = series is None
    if series is not None:
        issues = repository.list_coin_issues(series.id, cn=True)
        values = {v.numista_id: v for v in repository.list_coin_issue_values(numista_id)}
        rows = [(issue_row, values.get(issue_row.numista_id)) for issue_row in issues]
        currency = next((v.currency for v in values.values() if v.currency), None)
    else:
        candidate = repository.get_coin_explore_candidate(numista_id)
        if candidate is None:
            return templates.TemplateResponse(
                request,
                "coins/series.html",
                {"request": request, "page_title": "系列不存在", "series": None},
                status_code=404,
            )
        now = repository.now_local()
        series = coins_explore._series_from_payload(candidate.payload_cn, now)
        issues = [
            coins_explore._issue_from_payload(item, 0, now)
            for item in candidate.issues
            if item.get("numista_id") is not None
        ]
        rows = [(issue_row, None) for issue_row in issues]
        values = {}
        currency = None

    issue_years = {item.numista_id: item.year for item in issues}
    sales = _series_sales(numista_id, issue_years, currency)
    # 中位数按「币种 + 单版」分组：同一系列内不同币种的成交价不混算
    buckets: dict[tuple[str, int], list[float]] = {}
    for sale in sales:
        if sale["issue_numista_id"] is None:
            continue
        buckets.setdefault(
            (sale["currency"] or "", sale["issue_numista_id"]), []
        ).append(sale["price"])
    issue_medians = {key[1]: statistics.median(prices) for key, prices in buckets.items()}

    sort = "price" if sort == "price" else "sold"
    order = "asc" if order == "asc" else "desc"
    visible = [item for item in sales if item["issue_numista_id"] == issue] if issue is not None else sales
    if sort == "price":
        visible = sorted(visible, key=lambda item: item["price"], reverse=order != "asc")
    else:
        visible = sorted(visible, key=lambda item: item["sold_at"] or "", reverse=order != "asc")
    per_page = 5
    page_count = max(1, ceil(len(visible) / per_page))
    current_page = min(max(page, 1), page_count)
    page_sales = visible[(current_page - 1) * per_page: current_page * per_page]

    # 链接参数：排序 / 清除筛选 / 翻页都带上当前的其他条件
    base = {"sort": sort, "order": order}
    if issue is not None:
        base["issue"] = issue
    price_order = "asc" if sort == "price" and order == "desc" else "desc"
    sold_order = "asc" if sort == "sold" and order == "desc" else "desc"
    # 图片：Numista 的图不允许跨站引用，只有本地缓存过的才能 <img>，否则给原图链接
    has_obverse_image = coins_numista_web.cached_image(numista_id, "obverse") is not None
    has_reverse_image = coins_numista_web.cached_image(numista_id, "reverse") is not None
    # 补全信息按钮只在节点缺失时出现（与采集队列的「已有 → skip」口径一致）
    info_complete = bool(issues) and bool(values) and has_obverse_image and has_reverse_image
    # 右上角状态与币库列表一致：已收藏 > 已观察 > 未观察（互斥）
    is_collected = not is_candidate and any(
        member.numista_series_id == numista_id
        for member in repository.list_coin_collection_members()
    )
    is_watched = (
        not is_candidate
        and not is_collected
        and any(
            item.numista_id == numista_id
            for item in repository.list_coin_watchlist()
        )
    )

    return templates.TemplateResponse(
        request,
        "coins/series.html",
        {
            "request": request,
            "page_title": f"铜币系列 {series.title or numista_id}",
            "series": series,
            "rows": rows,
            "is_candidate": is_candidate,
            "is_collected": is_collected,
            "is_watched": is_watched,
            "return_to": f"/coins/series/{numista_id}",
            # 年份数 ≠ 版别数：一个年份可有多个版别（厂标 / 变体 / 精制），故分别给出
            "year_total": len({issue.year for issue in issues if issue.year is not None}),
            "issue_total": len(issues),
            "value_total": len(values),
            "currency": currency,
            "has_obverse_image": has_obverse_image,
            "has_reverse_image": has_reverse_image,
            "info_complete": info_complete,
            # 麦稀奇成交：中位数按「币种 + 单版」分组，筛选 / 排序 / 分页全走 query 参数
            "issue_medians": issue_medians,
            "issue_years": issue_years,
            "sales": page_sales,
            "sales_total": len(visible),
            "sales_pages": page_count,
            "sales_page": current_page,
            "issue_filter": issue,
            "sale_sort": sort,
            "sale_order": order,
            "sort_queries": {
                "price": urlencode({**base, "sort": "price", "order": price_order}),
                "sold": urlencode({**base, "sort": "sold", "order": sold_order}),
            },
            "clear_query": urlencode({"sort": sort, "order": order}),
            "page_queries": [
                urlencode({**base, "page": n}) for n in range(1, page_count + 1)
            ],
            "prev_query": urlencode({**base, "page": max(1, current_page - 1)}),
            "next_query": urlencode({**base, "page": min(page_count, current_page + 1)}),
        },
    )


def _series_sales(
    numista_id: int,
    issue_years: dict[int, int | None],
    series_currency: str | None,
) -> list[dict]:
    """已映射的麦稀奇成交 → 模板行（服务端渲染用）。

    拍品名称取 `coin_lots.title`，专场名称取 `coin_sessions.title`；
    拍品年份优先 `coin_lots.tags.发行年份`，缺失时回落单版年份。
    """
    rows = []
    for row in repository.list_coin_series_sales(numista_id):
        try:
            images = json.loads(row["image_urls"] or "[]")
        except (TypeError, ValueError):
            images = []
        try:
            tags = json.loads(row["tags"] or "{}")
        except (TypeError, ValueError):
            tags = {}
        year = str(tags.get("发行年份") or "").strip() or issue_years.get(row["issue_numista_id"])
        rows.append(
            {
                "lot_id": row["lot_id"],
                "title": row["lot_title"],
                "session_title": row["session_title"],
                "shop_name": row["shop_name"],
                "issue_numista_id": row["issue_numista_id"],
                "currency": row["currency"] or series_currency,
                "price": float(row["price"]),
                "sold_at": row["sold_at"],
                "sold_at_display": (row["sold_at"] or "").replace("T", " ")[:16],
                "year": year,
                "images": images[:2] if isinstance(images, list) else [],
            }
        )
    return rows


@router.post("/coins/series/{numista_id}/issue-fields")
def save_series_issue_fields(
    numista_id: int,
    issue_series: str = Form(""),
    issue_theme: str = Form(""),
    return_to: str = Form(""),
) -> Response:
    """保存系列详情页人工维护的发行系列 / 发行主题。"""
    if not repository.update_coin_series_issue_fields(
        numista_id, issue_series=issue_series, issue_theme=issue_theme
    ):
        return Response(status_code=404)
    return _redirect(return_to or f"/coins/series/{numista_id}")


@router.get("/coins/series/{numista_id}/image/{side}")
def series_image(numista_id: int, side: str) -> Response:
    """读本地缓存的系列正 / 反图（由 `coins-images` 下载到 `data/coin_images/`）。"""
    if side not in coins_numista_web.IMAGE_SIDES:
        return Response(status_code=404)
    path = coins_numista_web.cached_image(numista_id, side)
    if path is None:
        return Response(status_code=404)
    return FileResponse(path)


@router.get("/coins/manage")
def manage_page(
    request: Request,
    country: str = "",
    year: str = "",
    denomination: str = "",
    composition: str = "",
) -> Response:
    """采集管理页：两步式（按钮 1 取系列 + 结果行内按钮 2 取年份）。"""
    return _render_manage(request, country, year, denomination, composition, "")


@router.post("/coins/manage/fetch-series")
async def fetch_series(
    request: Request,
    country: str = Form(""),
    year: str = Form(""),
    denomination: str = Form(""),
    composition: str = Form(""),
) -> Response:
    """按钮 1：按「国家 + 年份 + 面值」调 Numista 采集系列（同步等待，失败就地提示）。

    面值 / 材质会在**取详情后**做精确过滤（Numista 的 q 是模糊搜索，见 `collect_series`），
    不匹配的系列不落库。
    """
    parsed_year, year_error = _parse_year(year)
    if year_error:
        return _render_manage(request, country, year, denomination, composition, year_error)
    if not country.strip():
        return _render_manage(
            request, country, year, denomination, composition, "请选择国家（下拉里的已收录国家）"
        )
    if parsed_year is None:
        return _render_manage(request, country, year, denomination, composition, "年份不能为空")
    if not denomination.strip():
        return _render_manage(
            request, country, year, denomination, composition, "面值不能为空，如「1便士」"
        )
    try:
        await coins_numista.collect_series(
            config.get(),
            country=country.strip(),
            year=parsed_year,
            denomination=denomination.strip(),
            composition=composition.strip(),
        )
    except NumistaError as exc:
        logger.warning("获取系列失败：%s", exc)
        return _render_manage(request, country, year, denomination, composition, str(exc))
    return _redirect(_manage_url(country, year, denomination, composition))


@router.post("/coins/manage/fetch-series-by-id")
async def fetch_series_by_id(request: Request, series_id: str = Form("")) -> Response:
    """按 Numista 系列 ID 直接取系列详情并入库（不按国家 / 年份 / 面值过滤）。"""
    text = series_id.strip()
    if not text.isdigit():
        return _render_manage(request, "", "", "", "", "系列 ID 必须是数字")
    try:
        await coins_numista.collect_series_by_id(config.get(), series_id=int(text))
    except NumistaError as exc:
        logger.warning("按 ID 获取系列失败：%s", exc)
        return _render_manage(request, "", "", "", "", str(exc))
    return _redirect(f"/coins/series/{int(text)}")


@router.post("/coins/manage/fetch-issues")
async def fetch_issues(
    request: Request,
    series_id: int = Form(...),
    country: str = Form(""),
    year: str = Form(""),
    denomination: str = Form(""),
    composition: str = Form(""),
    return_to: str = Form(""),
) -> Response:
    """按钮 2：按系列号取该系列全部年份的单版（同步等待，失败就地提示）。"""
    try:
        await coins_numista.collect_issues(config.get(), series_id=series_id)
    except NumistaError as exc:
        logger.warning("获取单版失败：%s", exc)
        return _render_manage(request, country, year, denomination, composition, str(exc))
    return _redirect(
        _return_target(return_to, _manage_url(country, year, denomination, composition))
    )


@router.post("/coins/manage/fetch-values")
async def fetch_values(
    request: Request,
    series_id: int = Form(...),
    country: str = Form(""),
    year: str = Form(""),
    denomination: str = Form(""),
    composition: str = Form(""),
    return_to: str = Form(""),
) -> Response:
    """按钮 3：按系列号取类型页各品相估值 + Frequency（按钮 2 的延伸，同步等待，失败就地提示）。

    估值走类型页，需本机开着带调试端口的 Chrome（CDP 链路，见 spec §14.13 v1.4）——
    与 CLI `coins-values` 同一函数，故报错提示也一致（`NumistaPageError` 自带开 Chrome 的指引）。
    """
    try:
        await coins_numista_web.collect_issue_values(config.get(), series_id=series_id)
    except NumistaPageError as exc:
        logger.warning("获取估值失败：%s", exc)
        return _render_manage(request, country, year, denomination, composition, str(exc))
    return _redirect(
        _return_target(return_to, _manage_url(country, year, denomination, composition))
    )


@router.post("/coins/manage/download-images")
async def download_images(
    request: Request,
    series_id: int = Form(...),
    country: str = Form(""),
    year: str = Form(""),
    denomination: str = Form(""),
    composition: str = Form(""),
    return_to: str = Form(""),
) -> Response:
    """按钮 4：按系列号把正 / 反图缓存到本地（同步等待，失败就地提示）。

    与 CLI `coins-images` 同一函数，同样需要带调试端口的 Chrome；下载后详情页 / 信息页
    才渲染本地图（Numista 图片不允许跨站引用，见 spec §14.15）。
    """
    try:
        await coins_numista_web.download_series_images(config.get(), numista_id=series_id)
    except NumistaPageError as exc:
        logger.warning("下载图片失败：%s", exc)
        return _render_manage(request, country, year, denomination, composition, str(exc))
    return _redirect(
        _return_target(return_to, _manage_url(country, year, denomination, composition))
    )


# ---------------------------------------------------------------- 历史购买品相录入（spec §14.16）
# 历史订单（coin_orders）按「国家 + 年份 + 面值」去重后，人工为每个组合录一档品相。
# 品相存独立表 coin_purchase_grades，不覆盖订单里 LLM 抽的 condition，也不影响画像 / 推荐。
# 年份 / 面值按 Numista 口径统一（去重键与落库键都是统一后的值，见 `app.coins.grades`）。


# ---------------------------------------------------------------- 采集中心（异步队列）

INGEST_NODE_LABELS = (
    ("系列", "node_series"),
    ("年份", "node_years"),
    ("估值", "node_values"),
    ("图片", "node_images"),
)

INGEST_STATE_LABELS = {
    "wait": "等待",
    "running": "进行中",
    "done": "完成",
    "skip": "跳过",
    "failed": "失败",
}

INGEST_TASK_STATUS_LABELS = {
    "pending": "待执行",
    "running": "进行中",
    "done": "完成",
    "failed": "失败",
}

# 队列表格的状态徽标按原型文案（查询下拉沿用 `INGEST_TASK_STATUS_LABELS`）。
INGEST_TASK_BADGE_LABELS = {
    "pending": "未开始",
    "running": "进行中",
    "done": "完成",
    "failed": "失败",
}

INGEST_RESULT_PAGE_SIZE = 20


async def _ingest_result_series(
    country: str, year: str, denomination: str, composition: str, series_id: str
) -> tuple[list, str]:
    """条件方式与 ID 方式实时调 Numista，结果暂存候选池后合并去重、按系列 ID 排序。

    ID 方式先查本地：币库已有直接提示跳过，候选池已有直接复用，均不调接口。
    """
    raw_id = series_id.strip()
    parsed_year, _ = _parse_year(year) if year else (None, "")
    found: dict[int, object] = {}
    error = ""
    if country.strip() or denomination.strip() or parsed_year is not None:
        try:
            for series in await coins_numista.explore_series(
                config.get(),
                country=country,
                year=parsed_year,
                denomination=denomination,
                composition=composition,
            ):
                found[series.numista_id] = series
        except NumistaError as exc:
            logger.warning("采集中心条件探索失败：%s", exc)
            error = str(exc)
    if raw_id.isdigit():
        numista_id = int(raw_id)
        candidate = repository.get_coin_explore_candidate(numista_id)
        if repository.get_coin_series(numista_id) is not None:
            note = f"系列 {numista_id} 已在币库，未调用接口"
        elif candidate is not None and candidate.payload_cn:
            found.setdefault(numista_id, coins_numista.SeriesInfo(**candidate.payload_cn))
            note = f"系列 {numista_id} 已在候选池，未调用接口"
        else:
            note = ""
            try:
                series = await coins_numista.explore_series_by_id(
                    config.get(), series_id=numista_id
                )
                found.setdefault(series.numista_id, series)
            except NumistaError as exc:
                logger.warning("采集中心按 ID 探索失败：%s", exc)
                error = error or str(exc)
        if note:
            error = f"{error}；{note}" if error else note
    return [found[numista_id] for numista_id in sorted(found)], error


@router.get("/coins/ingest")
async def ingest_page(
    request: Request,
    country: str = "",
    year: str = "",
    denomination: str = "",
    composition: str = "",
    series_id: str = "",
    batch: str = "",
    epage: str = "",
    page: str = "",
    series: str = "",
    name: str = "",
    status: str = "",
    created: str = "",
    finished: str = "",
) -> Response:
    """采集中心：条件 / ID 两种方式调 Numista 探索，结果入候选池后多选入队。"""
    if batch:
        queued_ids = {
            task.series_numista_id
            for task in repository.list_coin_ingest_tasks()
            if task.status in ("pending", "running")
        }
        results = [
            {
                "numista_id": item.numista_id,
                "title": item.title_cn or item.title,
                "issuer": item.issuer_cn or item.issuer,
                "ruler": item.ruler_cn or item.ruler,
                "denomination": item.denomination_cn or item.denomination,
                "min_year": item.min_year,
                "max_year": item.max_year,
                "composition": item.composition_cn or item.composition,
                "size_mm": item.size_mm,
            }
            for item in repository.list_coin_page_explore_items(batch)
            if item.numista_id not in queued_ids
        ]
        explore_error = ""
    else:
        results, explore_error = await _ingest_result_series(
            country, year, denomination, composition, series_id
        )
    paged_results, result_page, result_pages = _paginate(
        results, epage, INGEST_RESULT_PAGE_SIZE
    )
    tasks = repository.list_coin_ingest_tasks(
        series_numista_id=int(series) if series.strip().isdigit() else None,
        name=name or None,
        status=status or None,
        created_from=created or None,
        created_to=f"{created}T23:59" if created else None,
        finished_from=finished or None,
        finished_to=f"{finished}T23:59" if finished else None,
    )
    paged_tasks, task_page, task_pages = _paginate(tasks, page, INGEST_RESULT_PAGE_SIZE)
    _, year_error = _parse_year(year) if year else (None, "")
    explore_params = {
        "country": country,
        "year": year,
        "denomination": denomination,
        "composition": composition,
        "series_id": series_id,
        "batch": batch,
    }
    queue_params = {
        "series": series,
        "name": name,
        "status": status,
        "created": created,
        "finished": finished,
    }
    keep_params = {**explore_params, **queue_params}
    return templates.TemplateResponse(
        request,
        "coins/ingest.html",
        {
            "request": request,
            "page_title": "采集中心",
            "running": coins_ingest.is_running(),
            "node_labels": INGEST_NODE_LABELS,
            "state_labels": INGEST_STATE_LABELS,
            "status_labels": INGEST_TASK_STATUS_LABELS,
            "badge_labels": INGEST_TASK_BADGE_LABELS,
            "year_error": year_error,
            "country": country,
            "year": year,
            "denomination": denomination,
            "composition": composition,
            "series_id": series_id,
            "batch": batch,
            "page_explore_options": coins_explore_page.options(),
            "page_explore_batch": batch,
            "explore_error": explore_error,
            "results": paged_results,
            "result_total": len(results),
            "result_page": result_page,
            "result_pages": result_pages,
            "result_prev_url": (
                _query_url("/coins/ingest", {**keep_params, "epage": result_page - 1})
                if result_page > 1
                else ""
            ),
            "result_next_url": (
                _query_url("/coins/ingest", {**keep_params, "epage": result_page + 1})
                if result_page < result_pages
                else ""
            ),
            "tasks": paged_tasks,
            "task_total": len(tasks),
            "task_page": task_page,
            "task_pages": task_pages,
            "queue_series": series,
            "queue_name": name,
            "queue_status": status,
            "queue_created": created,
            "queue_finished": finished,
            "task_prev_url": (
                _query_url("/coins/ingest", {**keep_params, "page": task_page - 1})
                if task_page > 1
                else ""
            ),
            "task_next_url": (
                _query_url("/coins/ingest", {**keep_params, "page": task_page + 1})
                if task_page < task_pages
                else ""
            ),
            "queued": request.query_params.get("queued", ""),
            "reused": request.query_params.get("reused"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/coins/ingest/page-explore")
async def page_explore_start(
    issuer: str = Form(""),
    st: str = Form("1"),
    year: str = Form(""),
    material: str = Form(""),
    size: str = Form(""),
    start: str = Form("1"),
) -> Response:
    """批量方式(页面)：校验条件、后端重算链接；`start=1` 时起后台抓取任务。"""
    try:
        filters = coins_explore_page.normalize_filters(
            issuer=issuer, st=st, year=year, material=material, size=size
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if start not in ("1", "true", "on", "yes"):
        return JSONResponse(
            {
                "job_id": "",
                "url": coins_explore_page.catalogue_page_url(**filters),
                "batch_id": "",
            }
        )
    started = coins_explore_page.start(config.get(), **filters)
    if started is None:
        return JSONResponse({"error": "已有采集任务在跑"}, status_code=409)
    return JSONResponse(started)


@router.get("/coins/ingest/page-explore/status")
async def page_explore_status() -> Response:
    return JSONResponse(coins_explore_page.status())


@router.post("/coins/ingest")
async def enqueue_ingest(
    series_numista_id: str = Form(""), name: str = Form("")
) -> Response:
    """入队一个系列并在后台起队列；同系列已在队列时复用（提示回去）。"""
    raw = series_numista_id.strip()
    if not raw.isdigit():
        return _redirect("/coins/ingest?error=1")
    series = repository.get_coin_series(int(raw), cn=True)
    task_name = coins_ingest.series_task_name(series) or name.strip()
    _, reused = repository.enqueue_coin_ingest_task(
        int(raw), name=task_name, source="web"
    )
    coins_ingest.start(config.get())
    return _redirect("/coins/ingest?reused=1" if reused else "/coins/ingest")


@router.post("/coins/ingest/queue")
async def enqueue_ingest_selected(
    series_numista_id: list[str] = Form([]), batch: str = Form("")
) -> Response:
    """探索结果多选入队：逐个入队（已在队列的复用），回本页带数量提示。"""
    ids = sorted({int(raw) for raw in series_numista_id if raw.strip().isdigit()})
    if not ids:
        return _redirect("/coins/ingest?error=1")
    names = {
        series.numista_id: coins_ingest.series_task_name(series)
        for series in repository.search_coin_series(numista_ids=ids, cn=True)
    }
    page_items = (
        {
            item.numista_id: item
            for item in repository.list_coin_page_explore_items(batch.strip())
        }
        if batch.strip()
        else {}
    )
    queued = reused = 0
    for numista_id in ids:
        name = names.get(numista_id, "")
        if not name:
            candidate = repository.get_coin_explore_candidate(numista_id)
            if candidate is not None:
                payload = candidate.payload_cn or candidate.payload
                name = coins_ingest.series_task_name(payload)
        item = page_items.get(numista_id)
        if not name and item is not None:
            name = coins_ingest.series_task_name(
                {
                    "issuer": item.issuer_cn or item.issuer,
                    "ruler": item.ruler_cn or item.ruler,
                    "denomination": item.denomination_cn or item.denomination,
                    "min_year": item.min_year,
                    "max_year": item.max_year,
                    "title": item.title_cn or item.title,
                }
            )
        _, exists = repository.enqueue_coin_ingest_task(
            numista_id, name=name, source="explore"
        )
        reused += int(exists)
        queued += int(not exists)
    coins_ingest.start(config.get())
    location = f"/coins/ingest?queued={queued}&reused={reused}"
    if batch.strip():
        location = f"{location}&{urlencode({'batch': batch.strip()})}"
    return _redirect(location)


@router.post("/coins/ingest/delete")
async def delete_ingest_task(task_id: str = Form("")) -> Response:
    """从任务队列删除一条任务（按任务 ID）。"""
    if task_id.strip().isdigit():
        repository.delete_coin_ingest_task(int(task_id))
    return _redirect("/coins/ingest")


def _grades_url(country: str, year: str, denomination: str) -> str:
    query = urlencode({"country": country, "year": year, "denomination": denomination})
    return f"/coins/grades?{query}"


def _normalize_grade(
    country: str, year: str, denomination: str, theme: str = "", coin_name: str = ""
) -> tuple[str, str, str, str, str]:
    """把「国家 / 年份 / 面值 / 主题 / 名称」统一成落库口径（与页面展示、去重键同一份实现）。"""
    return coins_grades.normalize_key(country, year, denomination, theme, coin_name)


def _grades_context(country: str, year: str, denomination: str, grade: str) -> dict:
    """品相台账页与局部结果片段共用的上下文（筛选条件 + 去重组合 + 摘要计数）。"""
    all_combos = coins_grades.build_combos(
        repository.list_coin_orders(), repository.list_coin_purchase_grades()
    )
    combos = coins_grades.filter_combos(
        all_combos,
        country=country or None,
        era=year or None,
        denomination=denomination or None,
        grade=grade or None,
    )
    return {
        "page_title": "个人收藏清单",
        "country": country,
        "year": year,
        "denomination": denomination,
        "grade": grade,
        "countries": coins_grades.countries_of(all_combos),
        "denominations": coins_grades.denominations_of(all_combos),
        "grades": COIN_PURCHASE_GRADES,
        "combos": combos,
        "graded_total": sum(1 for combo in combos if combo.grade),
        "order_total": repository.count_coin_orders(),
    }


@router.get("/coins/grades")
def grades_page(
    request: Request,
    country: str = "",
    year: str = "",
    denomination: str = "",
    grade: str = "",
) -> Response:
    """品相录入页：查去重后的历史购买组合（不调接口），供人工录品相。"""
    return templates.TemplateResponse(
        request,
        "coins/grades.html",
        {"request": request, **_grades_context(country, year, denomination, grade)},
    )


@router.post("/coins/grades/add")
def add_grade(
    request: Request,
    country: str = Form(...),
    year: str = Form(""),
    era_name: str = Form(""),
    denomination: str = Form(...),
    theme: str = Form(""),
    coin_name: str = Form(""),
    entry: str = Form(""),
    grade: str = Form(""),
    next: str = Form(""),
    fragment: str = Form(""),
    filter_country: str = Form(""),
    filter_year: str = Form(""),
    filter_denomination: str = Form(""),
    filter_grade: str = Form(""),
) -> Response:
    """手工新增一条历史订单里没有的组合（国家 + 年份 + 面值 + 品相四项必填，年号可空）。

    年号只用于年号纪年的年份换算（`年份=6` + `年号=明治` → 公历 1873）；
    年份本身是 4 位公历年时忽略年号。
    带 `fragment=1` 时（页面局部刷新）只返回结果片段，按当前筛选条件重查，不跳转。
    """
    if entry:
        # 收藏系列页：一个面板跨成员，年份下拉用「年份|主题|名称」定位唯一一条
        parts = entry.split("|")
        if len(parts) != 3:
            return Response(status_code=400)
        year, theme, coin_name = parts
    raw_year = year.strip()
    era = raw_year if not era_name.strip() or (len(raw_year) == 4 and raw_year.isdigit()) else f"{era_name.strip()}{raw_year}"
    key_country, key_era, key_denomination, key_theme, key_coin_name = _normalize_grade(
        country, era, denomination, theme, coin_name
    )
    if not (key_country and key_era and key_denomination):
        return Response(status_code=400)
    if grade not in COIN_PURCHASE_GRADES:
        return Response(status_code=400)
    repository.upsert_coin_purchase_grade(
        key_country, key_era, key_denomination, grade, key_theme, key_coin_name
    )
    if fragment:
        return templates.TemplateResponse(
            request,
            "coins/_grades_results.html",
            {
                "request": request,
                **_grades_context(
                    filter_country, filter_year, filter_denomination, filter_grade
                ),
            },
        )
    return _redirect(next if next.startswith("/coins/") else "/coins/grades")


@router.post("/coins/grades/delete")
def delete_combo(
    country: str = Form(...),
    era: str = Form(...),
    denomination: str = Form(...),
    theme: str = Form(""),
    coin_name: str = Form(""),
    filter_country: str = Form(""),
    filter_year: str = Form(""),
    filter_denomination: str = Form(""),
) -> Response:
    """删除整行（历史错误数据）：清掉该组合的全部历史订单 + 该组合的品相记录。

    库里存的是**原始**写法，故先按同一口径统一、再逐个比对订单（见 `grades.order_ids_for_combo`）。
    订单本身没有主题 / 名称，具名行的删除只清品相记录，不动订单。
    注意：重跑 `coins-parse-orders` 会把 `orders.xlsx` 里的订单重新导回来；画像标签也不会自动重算。
    """
    key_country, key_era, key_denomination, key_theme, key_coin_name = _normalize_grade(
        country, era, denomination, theme, coin_name
    )
    if not (key_country and key_era and key_denomination):
        return Response(status_code=400)
    order_ids = (
        []
        if (key_theme or key_coin_name)
        else coins_grades.order_ids_for_combo(
            repository.list_coin_order_keys(), key_country, key_era, key_denomination
        )
    )
    deleted = repository.delete_coin_orders(order_ids)
    repository.delete_coin_purchase_grade(
        key_country, key_era, key_denomination, key_theme, key_coin_name
    )
    logger.info(
        "删除品相录入整行：%s / %s / %s（连带历史订单 %d 条）",
        key_country,
        key_era,
        key_denomination,
        deleted,
    )
    return _redirect(_grades_url(filter_country, filter_year, filter_denomination))


@router.post("/coins/grades/save")
def save_grades(
    country: list[str] = Form(default=[]),
    era: list[str] = Form(default=[]),
    denomination: list[str] = Form(default=[]),
    theme: list[str] = Form(default=[]),
    coin_name: list[str] = Form(default=[]),
    grade: list[str] = Form(default=[]),
    filter_country: str = Form(""),
    filter_year: str = Form(""),
    filter_denomination: str = Form(""),
) -> Response:
    """保存录入的品相：一行一个组合（国家 / 年份 / 面值 / 品相对齐）。

    主题 / 名称是后加的列，旧表单不带这两个字段，缺失时按空串补齐。
    下拉选「未录入」（空串）→ 清除该组合的记录；其余按
    `(country, era, denomination, theme, coin_name)` upsert。
    任一行的键不完整或品相非法 → 400（整批不落库，避免半截写入）。
    """
    if not (len(country) == len(era) == len(denomination) == len(grade)):
        return Response(status_code=400)
    theme = theme + [""] * (len(grade) - len(theme))
    coin_name = coin_name + [""] * (len(grade) - len(coin_name))

    pending: list[tuple[str, str, str, str, str, str]] = []
    for raw_country, raw_era, raw_denomination, raw_theme, raw_coin_name, value in zip(
        country, era, denomination, theme, coin_name, grade
    ):
        key_country, key_era, key_denomination, key_theme, key_coin_name = _normalize_grade(
            raw_country, raw_era, raw_denomination, raw_theme, raw_coin_name
        )
        if not (key_country and key_era and key_denomination):
            return Response(status_code=400)
        if value and value not in COIN_PURCHASE_GRADES:
            return Response(status_code=400)
        pending.append(
            (key_country, key_era, key_denomination, key_theme, key_coin_name, value)
        )

    for key_country, key_era, key_denomination, key_theme, key_coin_name, value in pending:
        if value:
            repository.upsert_coin_purchase_grade(
                key_country, key_era, key_denomination, value, key_theme, key_coin_name
            )
        else:
            repository.delete_coin_purchase_grade(
                key_country, key_era, key_denomination, key_theme, key_coin_name
            )
    return _redirect(_grades_url(filter_country, filter_year, filter_denomination))


# ---------------------------------------------------------------- 收藏画像与映射（spec §14.17）

COIN_COLLECTION_PAGE_SIZE = 20
COIN_LIBRARY_PAGE_SIZE = 20
COIN_RECOMMEND_PAGE_SIZE = 3
COIN_SIMILAR_PAGE_SIZE = 3
COIN_BEST_PAGE_SIZE = 3
COIN_PROFILE_PAGE_SIZE = 10
COIN_MEMBER_CANDIDATE_LIMIT = 50
COIN_REASON_LABELS = {1: "稀缺年份", 2: "差品升级", 4: "年份匹配", 5: "面值探索"}
# 推荐原因筛选只管精准匹配（P1/P2）；相似推荐（P4/P5）常驻展示
COIN_REASON_FILTER_LABELS = {1: "稀缺年份", 2: "差品升级"}
COIN_RAIL_SESSION_LIMIT = 6
COIN_SESSION_PAGE_SIZE = 12
COIN_SEARCH_PAGE_SIZE = 20
COIN_OTHER_LOT_PAGE_SIZE = 20
COIN_WATCHLIST_PAGE_SIZE = 20


def _page_number(raw: str, total_pages: int) -> int:
    """页码参数 → 1..total_pages；非法值按第 1 页。"""
    try:
        page = int(raw)
    except (TypeError, ValueError):
        page = 1
    return min(max(page, 1), max(total_pages, 1))


def _paginate(items: list, raw_page: str, size: int) -> tuple[list, int, int]:
    total_pages = max(1, (len(items) + size - 1) // size)
    page = _page_number(raw_page, total_pages)
    start = (page - 1) * size
    return items[start : start + size], page, total_pages


def _query_url(path: str, params: dict[str, object], anchor: str = "") -> str:
    query = {key: value for key, value in params.items() if value not in (None, "", 0)}
    suffix = f"?{urlencode(query)}" if query else ""
    return f"{path}{suffix}" + (f"#{anchor}" if anchor else "")


def _collection_page_url(
    page: int,
    ppage: int,
    *,
    spage: int = 0,
    bpage: int = 0,
    series: str = "",
    reason: str = "",
    hit: str = "",
    anchor: str = "",
) -> str:
    return _query_url(
        "/coins/collection",
        {
            "page": page if page > 1 else 0,
            "ppage": ppage if ppage > 1 else 0,
            "spage": spage if spage > 1 else 0,
            "bpage": bpage if bpage > 1 else 0,
            "series": series,
            "reason": reason,
            "hit": hit,
        },
        anchor,
    )


def _collection_ruler(collection: CoinCollectionSeries) -> str:
    """收藏系列的执政者：优先显式字段，其次取「国家 · 执政者 · 面值」名称的第二段。"""
    if collection.ruler:
        return collection.ruler
    parts = [part.strip() for part in (collection.name or "").split("·")]
    if len(parts) >= 3 and parts[1] not in ("", "-", "—"):
        return parts[1]
    return ""


def _member_filters(
    collection: CoinCollectionSeries,
    series_by_country: dict[str, list[CoinSeries]],
    country_by_norm: dict[str, str],
    member_ids: set[int],
    country_param: str,
    ruler_param: str,
    denomination_param: str,
    title_param: str = "",
) -> dict[str, object]:
    """某一收藏系列的成员候选：国家 / 执政者 / 面值默认取收藏系列自身的值。

    已加入任一收藏系列的 series 不进入候选。
    """
    countries = sorted(series_by_country)
    default_country = country_by_norm.get(textmatch.normalize(collection.country))
    selected_country = country_param or default_country or collection.country
    if selected_country not in series_by_country:
        countries = sorted(set(countries) | {selected_country})

    in_country = series_by_country.get(selected_country, [])
    ruler_by_norm = {
        textmatch.normalize(item.ruler): item.ruler
        for item in in_country
        if item.ruler
    }
    rulers = sorted(ruler_by_norm.values())
    own_ruler = ruler_by_norm.get(textmatch.normalize(_collection_ruler(collection)))
    if ruler_param == "__all__":
        selected_ruler = ""
    elif ruler_param and ruler_param in rulers:
        selected_ruler = ruler_param
    elif ruler_param:
        selected_ruler = ""
    else:
        selected_ruler = own_ruler or ""

    in_scope = [
        item
        for item in in_country
        if not selected_ruler
        or textmatch.normalize(item.ruler) == textmatch.normalize(selected_ruler)
    ]

    denomination_by_norm = {
        textmatch.normalize(item.denomination): item.denomination
        for item in in_scope
        if item.denomination
    }
    denominations = sorted(denomination_by_norm.values())
    own_denomination = denomination_by_norm.get(textmatch.normalize(collection.denomination))
    if denomination_param and denomination_param in denominations:
        selected_denomination = denomination_param
    elif country_param or ruler_param:
        # 切换国家 / 执政者后旧面值通常不在新列表里：先回落本系列面值，再回落第一个面值
        selected_denomination = (
            own_denomination or (denominations[0] if denominations else collection.denomination)
        )
    else:
        selected_denomination = own_denomination or collection.denomination
    if selected_denomination not in denominations:
        denominations = sorted(set(denominations) | {selected_denomination})

    candidates = [
        item
        for item in in_scope
        if item.denomination
        and textmatch.normalize(item.denomination)
        == textmatch.normalize(selected_denomination)
        and item.numista_id not in member_ids
    ]
    candidates.sort(key=lambda item: (item.min_year or 9999, item.numista_id))
    if title_param:
        keyword = textmatch.normalize(title_param)
        candidates = [
            item for item in candidates if keyword in textmatch.normalize(item.title or "")
        ]
    return {
        "countries": countries,
        "country": selected_country,
        "rulers": rulers,
        "ruler": selected_ruler,
        "denominations": denominations,
        "denomination": selected_denomination,
        "title": title_param,
        "candidates": candidates[:COIN_MEMBER_CANDIDATE_LIMIT],
        "candidate_total": len(candidates),
        "truncated": len(candidates) > COIN_MEMBER_CANDIDATE_LIMIT,
    }


def _explore_options(rows: list[dict[str, object]], key: str) -> list[str]:
    """筛选下拉的取值：去掉空值、按中文原样去重排序。"""
    values = {
        str(row[key]).strip() for row in rows if row.get(key) and str(row[key]).strip()
    }
    return sorted(values)


@router.get("/coins/explore")
def explore_page(
    request: Request,
    country: str = "",
    ruler: str = "",
    denomination: str = "",
    status: str = "candidate",
) -> Response:
    """探索候选池：目录扫描得到的外部铜币系列（忽略的不再显示），可按状态 / 国家 / 执政者 / 面值筛选。"""
    rows = []
    for candidate in repository.list_coin_explore_candidates():
        info = candidate.payload_cn
        rows.append(
            {
                "numista_id": candidate.numista_id,
                "verified_at": candidate.verified_at,
                "issuer": info.get("issuer"),
                "ruler": info.get("ruler"),
                "denomination": info.get("denomination"),
                "min_year": info.get("min_year"),
                "max_year": info.get("max_year"),
                "composition": info.get("composition"),
                "shape": info.get("shape"),
                "size_mm": info.get("size_mm"),
                "weight_g": info.get("weight_g"),
                "status": candidate.status,
                "obverse_picture": info.get("obverse_picture"),
                "reverse_picture": info.get("reverse_picture"),
                # Numista 图不允许跨站引用：只有本地缓存过的才直接 <img>
                "has_obverse_image": coins_numista_web.cached_image(
                    candidate.numista_id, "obverse"
                )
                is not None,
                "has_reverse_image": coins_numista_web.cached_image(
                    candidate.numista_id, "reverse"
                )
                is not None,
            }
        )

    total = len(rows)
    countries = _explore_options(rows, "issuer")
    # 国家 / 执政者 / 面值可手输：按输入值精确过滤，候选值只作 datalist 提示
    selected_country = country.strip()
    in_country = [row for row in rows if not selected_country or row["issuer"] == selected_country]
    # 提示值逐级收窄：填了国家就只提示该国的执政者，再按执政者收窄面值
    rulers = _explore_options(in_country, "ruler")
    selected_ruler = ruler.strip()
    in_scope = [row for row in in_country if not selected_ruler or row["ruler"] == selected_ruler]
    denominations = _explore_options(in_scope, "denomination")
    selected_denomination = denomination.strip()
    # 状态默认只看「候选」，选「全部」（空值）才回显采纳项；忽略项本就不入池
    status_options = [item for item in COIN_EXPLORE_STATUSES if item != "ignored"]
    selected_status = status if status in ("", *status_options) else "candidate"
    candidates = [
        row
        for row in in_scope
        if (not selected_status or row["status"] == selected_status)
        and (not selected_denomination or row["denomination"] == selected_denomination)
    ]
    filters_active = bool(
        selected_country or selected_ruler or selected_denomination
    ) or selected_status != "candidate"
    return templates.TemplateResponse(
        request,
        "coins/explore.html",
        {
            "request": request,
            "page_title": "候选池",
            "candidates": candidates,
            "total": total,
            "countries": countries,
            "country": selected_country,
            "rulers": rulers,
            "ruler": selected_ruler,
            "denominations": denominations,
            "denomination": selected_denomination,
            "status": selected_status,
            "filters_active": filters_active,
            "statuses": COIN_EXPLORE_STATUSES,
            "status_labels": COIN_EXPLORE_STATUS_LABELS,
        },
    )


@router.post("/coins/explore/status")
def update_explore_status(
    numista_id: int = Form(...), status: str = Form(...), next: str = Form("")
) -> Response:
    """保存候选状态；采纳时同时写入本地系列表（信息页 / 采集管理页可见）。"""
    if status not in COIN_EXPLORE_STATUSES:
        return _redirect("/coins/explore")
    if status == "adopted":
        coins_explore.adopt_candidate(numista_id)
    else:
        repository.set_coin_explore_candidate_status(numista_id, status)
    target = next if next.startswith("/coins/") else "/coins/explore"
    return _redirect(target)


@router.get("/coins/mxiqi")
def mxiqi_page(request: Request) -> Response:
    """麦稀奇数据入口页：只做分区导航，业务页面沿用现有实现。"""
    return templates.TemplateResponse(
        request,
        "coins/mxiqi.html",
        {"request": request, "page_title": "麦稀奇数据"},
    )


@router.get("/coins/analysis")
def analysis_page(
    request: Request,
    category: str = "",
    days: str = "",
    shop: str = "",
    graded: str = "0",
) -> Response:
    """分类分析（plan §18.19.7）：只读 `coin_lots`，默认只看硬币 + 裸币。

    `category` 切二级分类；`days` 时间窗；`shop` 店铺；`graded=1` 才包含评级盒。
    """
    filters = repository.list_coin_categories("11000")
    codes = {row["code"] for row in filters}
    selected = category if category in codes else ""
    try:
        window = int(days) if days else None
    except ValueError:
        window = None
    shops = repository.list_coin_analysis_shops()
    shop_ids = {str(row["id"]) for row in shops}
    selected_shop = int(shop) if shop in shop_ids else None

    rows = repository.list_coin_lot_analysis_rows(
        category_code=selected or None,
        only_ungraded=graded != "1",
        days=window,
        shop_id=selected_shop,
    )
    report = coins_analysis.build_report(rows)
    return templates.TemplateResponse(
        request,
        "coins/analysis.html",
        {
            "request": request,
            "page_title": "分类分析",
            "totals": report.totals,
            "overview": report.overview,
            "prices": report.prices,
            "start_bands": report.start_bands,
            "shops": report.shops,
            "relist": report.relist,
            "mislabels": report.mislabels,
            "band_labels": report.band_labels,
            "categories": filters,
            "category": selected,
            "days": window or "",
            "shops_options": shops,
            "shop": selected_shop or "",
            "graded": graded == "1",
            "min_samples": coins_analysis.MIN_SAMPLES,
        },
    )


_SHOP_SORTS = {"score": "综合靠谱分", "rate": "成交率", "gap": "同标题价差", "lots": "上架量"}


def _sort_shops(shops: list[dict], sort: str) -> list[dict]:
    """榜单排序：无该指标的店铺沉底，同值按样本数兜底。"""
    if sort == "rate":
        return sorted(
            shops,
            key=lambda shop: (shop["rate"] is not None, shop["rate"] or 0.0, shop["samples"]),
            reverse=True,
        )
    if sort == "gap":
        return sorted(
            shops,
            key=lambda shop: (shop["gap"] is None, abs(shop["gap"] or 0.0), -shop["samples"]),
        )
    if sort == "lots":
        return sorted(shops, key=lambda shop: (shop["lots"], shop["samples"]), reverse=True)
    return sorted(
        shops,
        key=lambda shop: (shop["score"] is not None, shop["score"] or 0.0, shop["samples"]),
        reverse=True,
    )


@router.get("/coins/shops")
def shops_page(
    request: Request,
    days: str = "",
    category: str = "",
    graded: str = "0",
    follow: str = "0",
    sort: str = "score",
) -> Response:
    """店铺分析（handoff 2026-11-03）：只读 `coin_lots`，默认全部时间 + 仅裸币。

    「前往今日拍场」按该店当天 `auction_at` 的场次判断；当天没有就展示「今日无拍卖」。
    """
    cfg = config.get()
    filters = repository.list_coin_categories("11000")
    codes = {row["code"] for row in filters}
    selected = category if category in codes else ""
    try:
        window = int(days) if days else None
    except ValueError:
        window = None
    if window is not None and window <= 0:
        window = None
    rows = repository.list_coin_shop_analysis_rows(
        category_code=selected or None,
        only_ungraded=graded != "1",
        days=window,
    )
    shop_ids = {row["shop_id"] for row in rows if row["shop_id"] is not None}
    today = repository.list_coin_shop_today_sessions(shop_ids, cfg.app.tz)
    report = coins_shops.build_shop_report(rows, today)
    shops = [shop for shop in report.shops if follow != "1" or shop["followed"]]
    selected_sort = sort if sort in _SHOP_SORTS else "score"
    shops = _sort_shops(shops, selected_sort)
    return templates.TemplateResponse(
        request,
        "coins/shop_analysis.html",
        {
            "request": request,
            "page_title": "店铺分析",
            "shops": shops,
            "totals": {**report.totals, "shops": len(shops)},
            "categories": filters,
            "days": window or "",
            "category": selected,
            "graded": graded == "1",
            "follow": follow == "1",
            "sort": selected_sort,
            "sort_label": _SHOP_SORTS[selected_sort],
            "min_samples": coins_shops.MIN_SAMPLES,
        },
    )


@router.get("/coins/collection")
def collection_page(
    request: Request,
    page: str = "",
    ppage: str = "",
    spage: str = "",
    bpage: str = "",
    series: str = "",
    reason: str = "",
    hit: str = "1",
) -> Response:
    """收藏画像 + 精准匹配 / 相似推荐分段展示 + 当日拍品匹配结果。

    推荐原因只筛精准匹配（P1/P2）；相似推荐（P4/P5）常驻展示，只跟着系列筛选。
    「已命中」过滤精准匹配（含当日拍品）；「未命中」隐藏精准匹配。
    """
    cfg = config.get()
    profiles, items = coins_recommend.load_recommendations(cfg)

    def _apply_series(source: list) -> list:
        if not series:
            return source
        return [item for item in source if item.collection.collection_key == series]

    # 精准匹配 = P1 缺失年份 + P2 差品升级；相似推荐 = P4 年份匹配 + P5 面值探索，不计入命中。
    precise_items = [item for item in items if item.priority in (1, 2)]
    similar_items = [item for item in items if item.priority in (4, 5)]
    precise_view = _apply_series(precise_items)
    # 推荐原因只控制精准匹配；相似推荐常驻展示，只跟系列筛选联动
    if reason in ("1", "2"):
        precise_view = [item for item in precise_view if str(item.priority) == reason]
    similar_view = _apply_series(similar_items)
    if hit == "1":
        precise_view = [item for item in precise_view if item.matched_lots]
    elif hit == "0":
        precise_view = []

    filtered_total = len(precise_view) + len(similar_view)
    paged_items, page_no, total_pages = _paginate(
        precise_view, page, COIN_RECOMMEND_PAGE_SIZE
    )
    profile_items, profile_page, profile_total_pages = _paginate(
        profiles, ppage, COIN_PROFILE_PAGE_SIZE
    )
    similar_paged, similar_page, similar_total_pages = _paginate(
        similar_view, spage, COIN_SIMILAR_PAGE_SIZE
    )
    best_items = coins_best.load_best_lots(cfg, series=series)
    best_paged, best_page, best_total_pages = _paginate(
        best_items, bpage, COIN_BEST_PAGE_SIZE
    )

    item_pager = {
        "page": page_no,
        "total_pages": total_pages,
        "prev_url": _collection_page_url(
            page_no - 1,
            profile_page,
            series=series,
            reason=reason,
            hit=hit,
            anchor="recommendations",
        )
        if page_no > 1
        else "",
        "next_url": _collection_page_url(
            page_no + 1,
            profile_page,
            series=series,
            reason=reason,
            hit=hit,
            anchor="recommendations",
        )
        if page_no < total_pages
        else "",
    }

    profile_pager = {
        "page": profile_page,
        "total_pages": profile_total_pages,
        "prev_url": _collection_page_url(
            page_no,
            profile_page - 1,
            series=series,
            reason=reason,
            hit=hit,
            anchor="profiles",
        )
        if profile_page > 1
        else "",
        "next_url": _collection_page_url(
            page_no,
            profile_page + 1,
            series=series,
            reason=reason,
            hit=hit,
            anchor="profiles",
        )
        if profile_page < profile_total_pages
        else "",
    }

    similar_pager = {
        "page": similar_page,
        "total_pages": similar_total_pages,
        "prev_url": _collection_page_url(
            page_no,
            profile_page,
            spage=similar_page - 1,
            series=series,
            reason=reason,
            hit=hit,
            anchor="similar",
        )
        if similar_page > 1
        else "",
        "next_url": _collection_page_url(
            page_no,
            profile_page,
            spage=similar_page + 1,
            series=series,
            reason=reason,
            hit=hit,
            anchor="similar",
        )
        if similar_page < similar_total_pages
        else "",
    }

    best_pager = {
        "page": best_page,
        "total_pages": best_total_pages,
        "prev_url": _collection_page_url(
            page_no,
            profile_page,
            spage=similar_page,
            bpage=best_page - 1,
            series=series,
            reason=reason,
            hit=hit,
            anchor="best",
        )
        if best_page > 1
        else "",
        "next_url": _collection_page_url(
            page_no,
            profile_page,
            spage=similar_page,
            bpage=best_page + 1,
            series=series,
            reason=reason,
            hit=hit,
            anchor="best",
        )
        if best_page < best_total_pages
        else "",
    }

    today_lots = repository.list_coin_today_lots(cfg.app.tz)
    today_sessions: dict[int, dict] = {}
    for lot in today_lots:
        session = today_sessions.setdefault(
            lot.session_id,
            {
                "session_id": lot.session_id,
                "session_title": lot.session_title,
                "shop_name": lot.shop_name,
                "auction_at": lot.auction_at,
                "lot_count": 0,
                "hit_count": 0,
                "similar_count": 0,
                "shop_followed": lot.shop_followed,
            },
        )
        session["lot_count"] += 1

    precise_lot_ids: set[int] = set()
    precise_shops: set[str] = set()
    precise_seen: dict[int, set[int]] = {}
    for item in precise_items:
        for lot in item.matched_lots:
            if lot.id is not None:
                precise_lot_ids.add(lot.id)
            precise_shops.add(lot.shop_name or "未知店铺")
            session = today_sessions.get(lot.session_id)
            if session is None or lot.id is None:
                continue
            seen = precise_seen.setdefault(lot.session_id, set())
            if lot.id in seen:
                continue
            seen.add(lot.id)
            session["hit_count"] += 1

    similar_seen: dict[int, set[int]] = {}
    similar_lot_ids: set[int] = set()
    for item in similar_items:
        for lot in item.matched_lots:
            session = today_sessions.get(lot.session_id)
            if session is None or lot.id is None or lot.id in precise_lot_ids:
                continue
            seen = similar_seen.setdefault(lot.session_id, set())
            if lot.id in seen:
                continue
            seen.add(lot.id)
            similar_lot_ids.add(lot.id)
            session["similar_count"] += 1

    # 右侧拍场：精准匹配 > 相似推荐 > 已关注店铺 > 普通拍场，同层按截拍时间。
    today_session_list = sorted(
        today_sessions.values(),
        key=lambda session: (
            0
            if session["hit_count"]
            else 1
            if session["similar_count"]
            else 2
            if session["shop_followed"]
            else 3,
            -session["hit_count"],
            -session["similar_count"],
            session["auction_at"] or "",
            session["session_id"],
        ),
    )
    # 聚合面板只铺匹配 / 已关注场次，超出上限的与普通专场一起折叠为「其他专场」
    rail_sessions = [
        session
        for session in today_session_list
        if session["hit_count"] or session["similar_count"] or session["shop_followed"]
    ][:COIN_RAIL_SESSION_LIMIT]
    rail_ids = {session["session_id"] for session in rail_sessions}
    other_sessions = [
        session
        for session in today_session_list
        if session["session_id"] not in rail_ids
    ]
    other_session_total = len(other_sessions)
    other_hit_total = sum(
        session["hit_count"] + session["similar_count"] for session in other_sessions
    )
    matched_lot_total = len(precise_lot_ids)
    matched_shop_total = len(precise_shops)
    missing_year_total = sum(len(profile.missing_years) for profile in profiles)
    upgrade_year_total = sum(len(profile.upgrade_years) for profile in profiles)
    covered_year_total = sum(len(profile.owned_entries) for profile in profiles)

    return templates.TemplateResponse(
        request,
        "coins/collection.html",
        {
            "request": request,
            "page_title": "今日推荐",
            "profiles": profile_items,
            "profiles_total": len(profiles),
            "profile_pager": profile_pager,
            "precise_items": paged_items,
            "precise_total": len(precise_view),
            "similar_items": similar_paged,
            "similar_total": len(similar_view),
            "similar_pager": similar_pager,
            "best_items": best_paged,
            "best_total": len(best_items),
            "best_pager": best_pager,
            "item_pager": item_pager,
            "item_total": len(items),
            "filtered_total": filtered_total,
            "reason_labels": COIN_REASON_LABELS,
            "reason_filter_labels": COIN_REASON_FILTER_LABELS,
            "collection_options": list(
                {
                    profile.collection.collection_key: {
                        "key": profile.collection.collection_key,
                        "name": profile.collection.name,
                    }
                    for profile in profiles
                }.values()
            ),
            "filter_series": series,
            "filter_reason": reason,
            "filter_hit": hit,
            "min_results": cfg.coins.collection.min_results,
            "today_lots": today_lots,
            "today_sessions": today_session_list,
            "today_session_total": len(today_session_list),
            "rail_sessions": rail_sessions,
            "other_session_total": other_session_total,
            "other_hit_total": other_hit_total,
            "similar_lot_total": len(similar_lot_ids),
            "today_date": datetime.now(cfg.app.tz).date().isoformat(),
            "matched_lot_total": matched_lot_total,
            "matched_shop_total": matched_shop_total,
            "missing_year_total": missing_year_total,
            "upgrade_year_total": upgrade_year_total,
            "covered_year_total": covered_year_total,
        },
    )


def _collections_redirect() -> Response:
    return _redirect("/coins/collections")


def _collection_grade_entries(profiles: list, series_by_id: dict[int, object]) -> list[dict]:
    """收藏系列品相面板的下拉：跨成员列出「年份(名称)」，value 携带定位键。

    一条收藏系列只保留一个「保存品相」面板；年份 + 发行系列 + 发行主题只用于定位唯一一条记录，
    真正落库的只有品相字段。
    面值取**成员**（Numista 系列）的面值 —— 同一收藏系列可能挂多个面值的成员；
    成员无面值时退回收藏系列的面值。
    """
    rows: list[dict] = []
    for profile in profiles:
        theme = (profile.issue_series or "").strip()
        coin_name = (profile.issue_theme or "").strip()
        member_denominations = [
            (series_by_id[series_id].denomination or "").strip()
            for series_id in profile.member_series_ids
            if series_id in series_by_id
        ]
        denomination = next(
            (value for value in member_denominations if value),
            profile.collection.denomination,
        )
        for entry in profile.entries:
            rows.append(
                {
                    "year": entry.year,
                    "label": entry.label,
                    "value": f"{entry.year}|{theme}|{coin_name}",
                    "status": entry.status,
                    "denomination": denomination,
                }
            )
    order = {"upgrade": 0, "missing": 1, "owned": 2}
    rows.sort(key=lambda row: (order.get(str(row["status"]), 3), int(row["year"])))
    for index, row in enumerate(rows):
        row["selected"] = index == 0
    return rows


def _collection_overview() -> dict:
    """收藏系列总览：系列 / 成员 / 画像 / 统计，列表页与详情页共用。"""
    collections = repository.list_coin_collection_series()
    members = repository.list_coin_collection_members()
    series = repository.list_coin_series(cn=True)
    series_by_id = {item.numista_id: item for item in series}
    members_by_collection: dict[int, list] = {
        collection.id or 0: [] for collection in collections
    }
    for member in members:
        members_by_collection.setdefault(member.collection_id, []).append(member)

    profiles = coins_collection.load_member_profiles()
    # 编辑区按成员展示品相录入；年份并排与统计用收藏系列级画像（按 (年份, 名称) 唯一）
    profiles_by_collection: dict[int, list[object]] = {}
    for profile in profiles:
        if profile.collection.id is not None:
            profiles_by_collection.setdefault(profile.collection.id, []).append(profile)
    grade_entries_by_collection = {
        collection_id: _collection_grade_entries(rows, series_by_id)
        for collection_id, rows in profiles_by_collection.items()
    }
    collection_profiles: dict[int, object] = {}
    collection_stats: dict[int, dict[str, object]] = {}
    for profile in coins_collection.merge_profiles(profiles, series_by_id):
        if profile.collection.id is None:
            continue
        collection_profiles[profile.collection.id] = profile
        collection_stats[profile.collection.id] = {
            "years": len(profile.entries),
            "owned": len(profile.owned_entries),
            "missing": len(profile.missing_entries),
            "upgrade": len(profile.upgrade_entries),
            "min_year": profile.min_year,
            "max_year": profile.max_year,
            "size_band": profile.size_band,
        }
    return {
        "collections": collections,
        "members": members,
        "series": series,
        "series_by_id": series_by_id,
        "members_by_collection": members_by_collection,
        "profiles": profiles,
        "profiles_by_collection": profiles_by_collection,
        "grade_entries_by_collection": grade_entries_by_collection,
        "collection_profiles": collection_profiles,
        "collection_stats": collection_stats,
    }


@router.get("/coins/collections")
def collections_page(
    request: Request,
    page: str = "",
    expand: str = "",
    country: str = "",
    ruler: str = "",
    denomination: str = "",
    mcountry: str = "",
    mruler: str = "",
    mdenomination: str = "",
    mtitle: str = "",
) -> Response:
    """收藏系列映射维护：新建收藏系列 + 把 Numista series 手工合并进来。"""
    overview = _collection_overview()
    collections = overview["collections"]
    members = overview["members"]
    all_collections = collections
    series = overview["series"]
    series_by_id = overview["series_by_id"]
    members_by_collection = overview["members_by_collection"]
    profiles = overview["profiles"]
    profiles_by_collection = overview["profiles_by_collection"]
    grade_entries_by_collection = overview["grade_entries_by_collection"]
    collection_profiles = overview["collection_profiles"]
    collection_stats = overview["collection_stats"]
    # 列表查询条：国家 / 执政者 / 面值（原型三项，模糊匹配）
    country_key = textmatch.normalize(country)
    ruler_key = textmatch.normalize(ruler)
    denomination_key = textmatch.normalize(denomination)
    if country_key or ruler_key or denomination_key:
        collections = [
            item
            for item in collections
            if (not country_key or country_key in textmatch.normalize(item.country))
            and (not ruler_key or ruler_key in textmatch.normalize(_collection_ruler(item)))
            and (
                not denomination_key
                or denomination_key in textmatch.normalize(item.denomination)
            )
        ]

    # 映射候选不限材质（青铜 / 铜 / 银 / 镍等都可选）
    series_candidates = list(series)

    try:
        expand_id = int(expand)
    except (TypeError, ValueError):
        expand_id = 0
    if expand_id:
        # 展开目标可能不在当前页：先定位到它所在页，再分页
        for index, collection in enumerate(collections):
            if collection.id == expand_id:
                page = str(index // COIN_COLLECTION_PAGE_SIZE + 1)
                break

    paged_collections, page_no, total_pages = _paginate(
        collections, page, COIN_COLLECTION_PAGE_SIZE
    )
    expanded_id = expand_id if any(item.id == expand_id for item in paged_collections) else 0

    series_by_country: dict[str, list[CoinSeries]] = {}
    for item in series_candidates:
        if item.issuer:
            series_by_country.setdefault(item.issuer, []).append(item)
    country_by_norm = {
        textmatch.normalize(country): country for country in series_by_country
    }

    all_member_ids = {member.numista_series_id for member in members}
    member_filters: dict[int, dict[str, object]] = {}
    for collection in paged_collections:
        is_expanded = collection.id == expanded_id
        member_filters[collection.id or 0] = _member_filters(
            collection,
            series_by_country,
            country_by_norm,
            all_member_ids,
            mcountry if is_expanded else "",
            mruler if is_expanded else "",
            mdenomination if is_expanded else "",
            mtitle if is_expanded else "",
        )

    filter_params = {
        "country": country,
        "ruler": ruler,
        "denomination": denomination,
    }
    prev_url = (
        _query_url("/coins/collections", {"page": page_no - 1, **filter_params})
        if page_no > 1
        else ""
    )
    next_url = (
        _query_url("/coins/collections", {"page": page_no + 1, **filter_params})
        if page_no < total_pages
        else ""
    )

    total_years = sum(len(profile.years) for profile in profiles)
    owned_years = sum(len(profile.owned_years) for profile in profiles)
    coverage_pct = round(owned_years / total_years * 100) if total_years else 0
    missing_year_total = sum(len(profile.missing_years) for profile in profiles)
    upgrade_year_total = sum(len(profile.upgrade_years) for profile in profiles)

    return templates.TemplateResponse(
        request,
        "coins/collections.html",
        {
            "request": request,
            "page_title": "收藏系列",
            "collections": paged_collections,
            "total_collections": len(collections),
            "total_members": sum(
                len(members_by_collection.get(item.id, [])) for item in collections
            ),
            "filter_country": country,
            "filter_ruler": ruler,
            "filter_denomination": denomination,
            "country_options": sorted(
                {item.country for item in all_collections if item.country}
            ),
            "ruler_options": sorted(
                {
                    value
                    for value in (_collection_ruler(item) for item in all_collections)
                    if value
                }
            ),
            "denomination_options": sorted(
                {item.denomination for item in all_collections if item.denomination}
            ),
            "page": page_no,
            "total_pages": total_pages,
            "prev_url": prev_url,
            "next_url": next_url,
            "expanded_id": expanded_id,
            "members_by_collection": members_by_collection,
            "series_by_id": series_by_id,
            "profile_by_id": profiles_by_collection,
            "profiles_by_collection": profiles_by_collection,
            "grade_entries_by_collection": grade_entries_by_collection,
            "collection_profiles": collection_profiles,
            "collection_stats": collection_stats,
            "series_candidates": series_candidates,
            "commemorative_ids": {
                item.numista_id
                for item in series
                if coins_collection.is_commemorative_object(item.object_type)
            },
            "member_filters": member_filters,
            "coverage_pct": coverage_pct,
            "missing_year_total": missing_year_total,
            "upgrade_year_total": upgrade_year_total,
            "grades": COIN_PURCHASE_GRADES,
        },
    )


@router.get("/coins/collections/{collection_id}")
def collection_detail_page(
    request: Request,
    collection_id: int,
    mcountry: str = "",
    mruler: str = "",
    mdenomination: str = "",
    mtitle: str = "",
) -> Response:
    """收藏系列详情：概况（进度 / 年份 / 品相录入）+ 成员管理 + 编辑 / 删除。"""
    collection = repository.get_coin_collection_series(collection_id)
    if collection is None:
        return Response(status_code=404)
    overview = _collection_overview()
    series = overview["series"]
    series_by_id = overview["series_by_id"]
    members = [
        member
        for member in overview["members"]
        if member.collection_id == collection_id
    ]
    all_member_ids = {
        member.numista_series_id for member in overview["members"]
    }
    series_by_country: dict[str, list[CoinSeries]] = {}
    for item in series:
        if item.issuer:
            series_by_country.setdefault(item.issuer, []).append(item)
    country_by_norm = {
        textmatch.normalize(country): country for country in series_by_country
    }
    filters = _member_filters(
        collection,
        series_by_country,
        country_by_norm,
        all_member_ids,
        mcountry,
        mruler,
        mdenomination,
        mtitle,
    )
    stats = overview["collection_stats"].get(collection_id, {})
    total_years = stats.get("years", 0)
    coverage_pct = (
        round(stats.get("owned", 0) * 100 / total_years) if total_years else 0
    )
    commemorative_ids = {
        item.numista_id
        for item in series
        if coins_collection.is_commemorative_object(item.object_type)
    }
    member_year_counts = {
        numista_id: issue_stats.year_count
        for numista_id, issue_stats in repository.coin_series_issue_stats(cn=True).items()
    }
    has_commemorative_member = any(
        member.numista_series_id in commemorative_ids for member in members
    )
    return templates.TemplateResponse(
        request,
        "coins/collection_detail.html",
        {
            "request": request,
            "page_title": collection.name,
            "collection": collection,
            "members": members,
            "total_members": len(members),
            "series_by_id": series_by_id,
            "profile": overview["collection_profiles"].get(collection_id),
            "stats": stats,
            "coverage_pct": coverage_pct,
            "filters": filters,
            "commemorative_ids": commemorative_ids,
            "has_commemorative_member": has_commemorative_member,
            "member_year_counts": member_year_counts,
        },
    )


@router.post("/coins/collections/add")
def add_collection(
    collection_key: str = Form(""),
    name: str = Form(""),
    country: str = Form(...),
    ruler: str = Form(""),
    denomination: str = Form(...),
    size_mm: str = Form(""),
) -> Response:
    country = country.strip()
    denomination = denomination.strip()
    if not country or not denomination:
        return Response(status_code=400)
    ruler_value = ruler.strip() or None
    size_value: float | None = None
    if size_mm.strip():
        try:
            size_value = float(size_mm)
        except ValueError:
            return Response(status_code=400)
    if collection_key.strip():
        key = collection_key.strip()
    else:
        key = f"{country}-{ruler_value or '-'}-{denomination}"
    display_name = name.strip() or " · ".join(
        part for part in (country, ruler_value or "-", denomination) if part
    )
    repository.upsert_coin_collection_series(
        collection_key=key,
        name=display_name,
        country=country,
        ruler=ruler_value,
        denomination=denomination,
        size_mm=size_value,
        size_band=coins_collection.size_band(size_value),
    )
    return _collections_redirect()


@router.post("/coins/collections/{collection_id}/edit")
def edit_collection(
    collection_id: int,
    name: str = Form(""),
    country: str = Form(...),
    ruler: str = Form(""),
    denomination: str = Form(...),
) -> Response:
    collection = repository.get_coin_collection_series(collection_id)
    if collection is None:
        return Response(status_code=404)
    country = country.strip()
    denomination = denomination.strip()
    if not country or not denomination:
        return Response(status_code=400)
    ruler_value = ruler.strip() or None
    display_name = name.strip() or " · ".join(
        part for part in (country, ruler_value or "-", denomination) if part
    )
    repository.upsert_coin_collection_series(
        collection_key=collection.collection_key,
        name=display_name,
        country=country,
        ruler=ruler_value,
        denomination=denomination,
        size_mm=collection.size_mm,
        size_band=collection.size_band,
    )
    return _redirect(f"/coins/collections/{collection_id}")


@router.post("/coins/collections/{collection_id}/delete")
def delete_collection(collection_id: int) -> Response:
    if not repository.delete_coin_collection_series(collection_id):
        return Response(status_code=404)
    return _collections_redirect()


@router.post("/coins/collections/members/add")
def add_collection_member(
    collection_id: int = Form(...),
    numista_series_id: int = Form(...),
    mcountry: str = Form(""),
    mruler: str = Form(""),
    mdenomination: str = Form(""),
    mtitle: str = Form(""),
) -> Response:
    series = repository.get_coin_series(numista_series_id, cn=True)
    if series is None:
        return Response(status_code=400)
    name = " · ".join(
        part for part in (series.issuer, series.ruler, series.denomination) if part
    ) or series.title
    repository.upsert_coin_collection_member(collection_id, numista_series_id, name)
    # 加入后回到该收藏系列详情，并带上已选国家 / 执政者 / 面值，避免条件被重置
    return _redirect(
        _query_url(
            f"/coins/collections/{collection_id}",
            {
                "mcountry": mcountry,
                "mruler": mruler,
                "mdenomination": mdenomination,
                "mtitle": mtitle,
            },
            anchor="member-panel",
        )
    )


@router.post("/coins/collections/members/{member_id}/delete")
def delete_collection_member(member_id: int, collection_id: int = Form(0)) -> Response:
    if not repository.delete_coin_collection_member(member_id):
        return Response(status_code=404)
    if collection_id:
        return _redirect(f"/coins/collections/{collection_id}#member-panel")
    return _collections_redirect()

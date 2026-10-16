"""铜币拍卖页面（spec §14.7）。

只做「参数校验 + 调 repository + 渲染模板」——采集与匹配在 `app/coins/*`，
本文件不写 SQL、不发网络请求（同 plan §3.2 的分层约定）。
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse
from starlette.responses import Response

from app import config, repository
from app.coins import grades as coins_grades
from app.coins import numista as coins_numista
from app.coins import numista_web as coins_numista_web
from app.coins import pipeline as coins_pipeline
from app.coins.numista import ISSUER_CODES, NumistaError
from app.coins.numista_web import NumistaPageError
from app.models import (
    COIN_DIMENSION_LABELS,
    COIN_PROFILE_DIMENSIONS,
    COIN_PURCHASE_GRADES,
    COIN_SESSION_STATUS_LABELS,
)
from app.routers import templates

logger = logging.getLogger(__name__)

router = APIRouter()

# 维度 / 状态的中文名放在 models 里（CLI 也要用，避免 CLI 反过来 import router）
DIMENSION_LABELS = COIN_DIMENSION_LABELS
SESSION_STATUS_LABELS = COIN_SESSION_STATUS_LABELS

# 手工触发的采集任务：留强引用，避免被 GC（Python 的标准做法）
_background_tasks: set[asyncio.Task] = set()


def format_price(value: float | None) -> str:
    """价格显示：整数不带小数点，缺失显示 `—`。"""
    if value is None:
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


templates.env.filters["price"] = format_price
templates.env.globals["coin_dimension_labels"] = DIMENSION_LABELS
templates.env.globals["coin_session_status_labels"] = SESSION_STATUS_LABELS


# ---------------------------------------------------------------- 第一页：专场列表


@router.get("/coins")
def sessions_page(request: Request) -> Response:
    cards = repository.list_coin_session_cards()
    return templates.TemplateResponse(
        request,
        "coins/sessions.html",
        {
            "request": request,
            "page_title": "铜币拍卖",
            "cards": cards,
            "summary": repository.coin_fetch_summary(),
        },
    )


# ---------------------------------------------------------------- 第二页：拍品页


@router.get("/coins/session/{session_id}")
def session_page(request: Request, session_id: int) -> Response:
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
    return templates.TemplateResponse(
        request,
        "coins/session.html",
        {
            "request": request,
            "page_title": detail.title,
            "detail": detail,
        },
    )


# ---------------------------------------------------------------- 画像：查看与编辑


@router.get("/coins/profile")
def profile_page(request: Request) -> Response:
    tags = repository.list_coin_profile_tags()
    grouped = [
        (dimension, DIMENSION_LABELS.get(dimension, dimension), [t for t in tags if t.dimension == dimension])
        for dimension in COIN_PROFILE_DIMENSIONS
    ]
    return templates.TemplateResponse(
        request,
        "coins/profile.html",
        {
            "request": request,
            "page_title": "铜币画像",
            "grouped": grouped,
            "tag_total": len(tags),
            "order_total": repository.count_coin_orders(),
            "dimensions": COIN_PROFILE_DIMENSIONS,
        },
    )


@router.post("/coins/profile/{tag_id}/toggle")
def toggle_tag(tag_id: int) -> Response:
    tag = repository.get_coin_tag(tag_id)
    if tag is None:
        return Response(status_code=404)
    repository.set_coin_tag_enabled(tag_id, not tag.enabled)
    return _redirect("/coins/profile")


@router.post("/coins/profile/{tag_id}/weight")
def update_weight(tag_id: int, weight: float = Form(...)) -> Response:
    if repository.get_coin_tag(tag_id) is None:
        return Response(status_code=404)
    repository.set_coin_tag_weight(tag_id, max(0.0, weight))
    return _redirect("/coins/profile")


@router.post("/coins/profile/{tag_id}/delete")
def delete_tag(tag_id: int) -> Response:
    if not repository.delete_coin_tag(tag_id):
        return Response(status_code=404)
    return _redirect("/coins/profile")


@router.post("/coins/profile/add")
def add_tag(
    dimension: str = Form(...),
    value: str = Form(...),
    weight: float = Form(1.0),
    note: str = Form(""),
) -> Response:
    if dimension not in COIN_PROFILE_DIMENSIONS:
        return Response(status_code=400)
    trimmed = value.strip()
    if not trimmed:
        return Response(status_code=400)
    repository.add_coin_tag(dimension, trimmed, max(0.0, weight), note.strip() or None)
    return _redirect("/coins/profile")


def _redirect(path: str) -> Response:
    return Response(status_code=302, headers={"Location": path})


async def _run_coins(cfg: config.Config) -> None:
    try:
        await coins_pipeline.run(cfg)
    except Exception:
        logger.exception("手工触发的铜币采集失败")


@router.post("/coins/refresh")
async def refresh_coins() -> Response:
    """手工触发一轮铜币采集 + 查价（调试 / 自愈）。

    后台任务、不 await：一轮要拉几十个专场、可能几分钟，不能让 HTTP 请求干等
    （与管理页「立即运行」同一取舍，plan §19）。刷新页面后看顶部「数据更新至」是否推进。
    采集与查价都走小程序网关（免登录、免浏览器）。
    """
    task = asyncio.create_task(_run_coins(config.get()))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return _redirect("/coins")


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
            "page_title": "铜币采集管理",
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
    country: str = "",
    year: str = "",
    denomination: str = "",
    composition: str = "",
) -> Response:
    """信息查询页：按国家 + 年份 + 面值 + 材质查**本地已采集**的系列（不调接口）。"""
    parsed_year, year_error = _parse_year(year)
    series_list = repository.search_coin_series(
        issuer=country or None,
        year=parsed_year,
        denomination=denomination or None,
        composition=composition or None,
        cn=True,
    )
    return templates.TemplateResponse(
        request,
        "coins/info.html",
        {
            "request": request,
            "page_title": "铜币信息",
            "countries": repository.list_coin_series_countries(cn=True),
            "country": country,
            "year": year,
            "denomination": denomination,
            "composition": composition,
            "denominations": repository.list_coin_series_denominations(cn=True),
            "compositions": repository.list_coin_series_compositions(cn=True),
            "year_error": year_error,
            "series_list": series_list,
            # 图片与详情页同规矩（spec §14.15）：Numista 图不允许跨站引用，
            # 列表卡里只渲染**已缓存到本地**的那一侧，其余给占位（卡片本身是 <a>，不能再嵌链接）。
            "series_images": {
                item.numista_id: {
                    side: coins_numista_web.cached_image(item.numista_id, side) is not None
                    for side in coins_numista_web.IMAGE_SIDES
                }
                for item in series_list
            },
            "issue_stats": repository.coin_series_issue_stats(cn=True),
            "total_series": repository.count_coin_series(cn=True),
        },
    )


@router.get("/coins/series/{numista_id}")
def series_page(request: Request, numista_id: int) -> Response:
    """系列详情：系列信息 + 各年份信息表 + 各年份估值表（估值只读展示）。"""
    series = repository.get_coin_series(numista_id, cn=True)
    if series is None:
        return templates.TemplateResponse(
            request,
            "coins/series.html",
            {"request": request, "page_title": "系列不存在", "series": None},
            status_code=404,
        )
    issues = repository.list_coin_issues(series.id, cn=True)
    values = {v.numista_id: v for v in repository.list_coin_issue_values(numista_id)}
    rows = [(issue, values.get(issue.numista_id)) for issue in issues]
    currency = next((v.currency for v in values.values() if v.currency), None)
    return templates.TemplateResponse(
        request,
        "coins/series.html",
        {
            "request": request,
            "page_title": f"铜币系列 {series.title or numista_id}",
            "series": series,
            "rows": rows,
            # 年份数 ≠ 版别数：一个年份可有多个版别（厂标 / 变体 / 精制），故分别给出
            "year_total": len({issue.year for issue in issues if issue.year is not None}),
            "issue_total": len(issues),
            "value_total": len(values),
            "currency": currency,
            # 图片：Numista 的图不允许跨站引用，只有本地缓存过的才能 <img>，否则给原图链接
            "has_obverse_image": coins_numista_web.cached_image(numista_id, "obverse") is not None,
            "has_reverse_image": coins_numista_web.cached_image(numista_id, "reverse") is not None,
        },
    )


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


@router.post("/coins/manage/fetch-issues")
async def fetch_issues(
    request: Request,
    series_id: int = Form(...),
    country: str = Form(""),
    year: str = Form(""),
    denomination: str = Form(""),
    composition: str = Form(""),
) -> Response:
    """按钮 2：按系列号取该系列全部年份的单版（同步等待，失败就地提示）。"""
    try:
        await coins_numista.collect_issues(config.get(), series_id=series_id)
    except NumistaError as exc:
        logger.warning("获取单版失败：%s", exc)
        return _render_manage(request, country, year, denomination, composition, str(exc))
    return _redirect(_manage_url(country, year, denomination, composition))


@router.post("/coins/manage/fetch-values")
async def fetch_values(
    request: Request,
    series_id: int = Form(...),
    country: str = Form(""),
    year: str = Form(""),
    denomination: str = Form(""),
    composition: str = Form(""),
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
    return _redirect(_manage_url(country, year, denomination, composition))


@router.post("/coins/manage/download-images")
async def download_images(
    request: Request,
    series_id: int = Form(...),
    country: str = Form(""),
    year: str = Form(""),
    denomination: str = Form(""),
    composition: str = Form(""),
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
    return _redirect(_manage_url(country, year, denomination, composition))


# ---------------------------------------------------------------- 历史购买品相录入（spec §14.16）
# 历史订单（coin_orders）按「国家 + 年份 + 面值」去重后，人工为每个组合录一档品相。
# 品相存独立表 coin_purchase_grades，不覆盖订单里 LLM 抽的 condition，也不影响画像 / 推荐。
# 年份 / 面值按 Numista 口径统一（去重键与落库键都是统一后的值，见 `app.coins.grades`）。


def _grades_url(country: str, year: str, denomination: str) -> str:
    query = urlencode({"country": country, "year": year, "denomination": denomination})
    return f"/coins/grades?{query}"


def _normalize_grade(country: str, year: str, denomination: str) -> tuple[str, str, str]:
    """把「国家 / 年份 / 面值」统一成落库口径（与页面展示、去重键同一份实现）。"""
    return (
        coins_grades.normalize_country(country),
        coins_grades.normalize_year(year),
        coins_grades.normalize_denomination(denomination),
    )


@router.get("/coins/grades")
def grades_page(
    request: Request,
    country: str = "",
    year: str = "",
    denomination: str = "",
    grade: str = "",
) -> Response:
    """品相录入页：查去重后的历史购买组合（不调接口），供人工录品相。"""
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
    return templates.TemplateResponse(
        request,
        "coins/grades.html",
        {
            "request": request,
            "page_title": "铜币品相录入",
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
        },
    )


@router.post("/coins/grades/add")
def add_grade(
    country: str = Form(...),
    year: str = Form(...),
    denomination: str = Form(...),
    grade: str = Form(""),
) -> Response:
    """手工新增一条历史订单里没有的组合（国家 + 年份 + 面值 + 品相四项必填）。"""
    key_country, key_era, key_denomination = _normalize_grade(country, year, denomination)
    if not (key_country and key_era and key_denomination):
        return Response(status_code=400)
    if grade not in COIN_PURCHASE_GRADES:
        return Response(status_code=400)
    repository.upsert_coin_purchase_grade(key_country, key_era, key_denomination, grade)
    return _redirect("/coins/grades")


@router.post("/coins/grades/delete")
def delete_combo(
    country: str = Form(...),
    era: str = Form(...),
    denomination: str = Form(...),
    filter_country: str = Form(""),
    filter_year: str = Form(""),
    filter_denomination: str = Form(""),
) -> Response:
    """删除整行（历史错误数据）：清掉该组合的全部历史订单 + 该组合的品相记录。

    库里存的是**原始**写法，故先按同一口径统一、再逐个比对订单（见 `grades.order_ids_for_combo`）。
    注意：重跑 `coins-parse-orders` 会把 `orders.xlsx` 里的订单重新导回来；画像标签也不会自动重算。
    """
    key_country, key_era, key_denomination = _normalize_grade(country, era, denomination)
    if not (key_country and key_era and key_denomination):
        return Response(status_code=400)
    order_ids = coins_grades.order_ids_for_combo(
        repository.list_coin_order_keys(), key_country, key_era, key_denomination
    )
    deleted = repository.delete_coin_orders(order_ids)
    repository.delete_coin_purchase_grade(key_country, key_era, key_denomination)
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
    grade: list[str] = Form(default=[]),
    filter_country: str = Form(""),
    filter_year: str = Form(""),
    filter_denomination: str = Form(""),
) -> Response:
    """保存录入的品相：一行一个组合（四个字段按行对齐）。

    下拉选「未录入」（空串）→ 清除该组合的记录；其余按 `(country, era, denomination)` upsert。
    任一行的键不完整或品相非法 → 400（整批不落库，避免半截写入）。
    """
    if not (len(country) == len(era) == len(denomination) == len(grade)):
        return Response(status_code=400)

    pending: list[tuple[str, str, str, str]] = []
    for raw_country, raw_era, raw_denomination, value in zip(country, era, denomination, grade):
        key_country, key_era, key_denomination = _normalize_grade(
            raw_country, raw_era, raw_denomination
        )
        if not (key_country and key_era and key_denomination):
            return Response(status_code=400)
        if value and value not in COIN_PURCHASE_GRADES:
            return Response(status_code=400)
        pending.append((key_country, key_era, key_denomination, value))

    for key_country, key_era, key_denomination, value in pending:
        if value:
            repository.upsert_coin_purchase_grade(key_country, key_era, key_denomination, value)
        else:
            repository.delete_coin_purchase_grade(key_country, key_era, key_denomination)
    return _redirect(_grades_url(filter_country, filter_year, filter_denomination))

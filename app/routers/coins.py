"""铜币拍卖页面（spec §14.7）。

只做「参数校验 + 调 repository + 渲染模板」——采集与匹配在 `app/coins/*`，
本文件不写 SQL、不发网络请求（同 plan §3.2 的分层约定）。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Form, Request
from starlette.responses import Response

from app import config, repository
from app.coins import pipeline as coins_pipeline
from app.models import (
    COIN_DIMENSION_LABELS,
    COIN_PROFILE_DIMENSIONS,
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
                "summary": repository.coin_fetch_summary(),
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
            "summary": repository.coin_fetch_summary(),
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

    后台任务、不 await：一轮要开浏览器、可能几分钟，不能让 HTTP 请求干等
    （同 `startup_fetch` 的取舍）。刷新页面后看顶部「数据更新至」是否推进。
    注意：会话被反爬拦下时，本机服务是 headless 的，**修不了** —— 得跑
    `python -m app.cli coins-fetch --headful` 人工过一次验证。
    """
    task = asyncio.create_task(_run_coins(config.get()))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return _redirect("/coins")

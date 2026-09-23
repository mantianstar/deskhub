"""首页日报与点击中转（plan §10）。

候选池定义落在 repository（`local_day_bounds` / `query_digest`），这里只决定
「今天没有就回退 48 小时并标注」这一步展示逻辑。

路由写成同步 `def`：sqlite3 是同步库，FastAPI 会把同步处理函数丢进线程池，
既不阻塞事件循环，也不用在路由里到处写 `asyncio.to_thread`（plan §6.3）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app import config, repository
from app.models import DigestItem
from app.routers import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _load_digest(module: str | None) -> tuple[list[DigestItem], bool]:
    """取日报条目；返回（条目, 是否回退窗口）。

    当日候选为 0（服务当天没跑过 / 源都没更新）时回退到 `digest.lookback_hours`
    内，由调用方在页面顶部标注「非今日数据」——宁可标注，也不要给一个空首页（plan §9.1）。
    """
    cfg = config.get()
    day_start, day_end = repository.local_day_bounds(cfg.app.tz)
    items = repository.query_digest(day_start, day_end, module, cfg.digest.limit)
    if items:
        return items, False

    lookback_start, lookback_end = repository.lookback_bounds(cfg.digest.lookback_hours)
    return repository.query_digest(lookback_start, lookback_end, module, cfg.digest.limit), True


def _digest_context(request: Request, module: str | None, page_title: str) -> dict[str, object]:
    cfg = config.get()
    items, is_fallback = _load_digest(module)
    return {
        "request": request,
        "page_title": page_title,
        "items": items,
        "is_fallback": is_fallback,
        "lookback_hours": cfg.digest.lookback_hours,
        "limit": cfg.digest.limit,
        # 首轮抓取还没结束时（last_fetch_at 为空），空首页要说清是「正在抓」而不是「今天没内容」
        "first_fetch_pending": repository.last_fetch_at() is None,
    }


@router.get("/")
def digest_page(request: Request) -> Response:
    """今日精选：全模块混合，取 `digest.limit` 条。"""
    return templates.TemplateResponse(
        request, "digest.html", _digest_context(request, None, "今日精选")
    )


@router.get("/go/{item_id}")
def go(item_id: int) -> RedirectResponse:
    """记一次点击后 302 跳外链；`item_id` 不存在返回 404。"""
    url = repository.get_item_url(item_id)
    if url is None:
        raise HTTPException(status_code=404, detail=f"条目不存在：id={item_id}")
    repository.mark_clicked(item_id)
    logger.info("点击跳转 item_id=%s url=%s", item_id, url)
    return RedirectResponse(url, status_code=302)

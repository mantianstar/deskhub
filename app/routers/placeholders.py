"""基金占位页（spec §2.2 / plan §10）：只有外壳，不写任何抓取或业务逻辑。

> 铜币拍卖已于 2026-10-09 升级为正式模块（见 `app/routers/coins.py`），不再走占位页。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.routers import templates

router = APIRouter()


def _placeholder(request: Request, title: str, note: str) -> Response:
    return templates.TemplateResponse(
        request,
        "placeholder.html",
        {"request": request, "page_title": title, "note": note},
    )


@router.get("/funds")
def funds_page(request: Request) -> Response:
    return _placeholder(
        request,
        "基金监控",
        "v1 只保留入口：这里没有任何行情或持仓数据，等知识聚合跑顺了再说。",
    )

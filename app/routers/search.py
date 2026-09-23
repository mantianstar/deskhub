"""历史搜索（plan §10）。

参数一律以字符串进来、在这里转成查询条件：页码、日期、module 都可能被手改 URL 写坏，
坏值一律降级成「不生效」而不是 500（M4-2 的验收要求）。

时间范围按**发布时间**折算（业务时区 → UTC，左闭右开），与卡片上显示的「xx 发布」同一个口径；
折算规则落在 repository 的 `day_bounds_utc`。
"""

from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from starlette.responses import Response

from app import config, repository
from app.routers import templates

router = APIRouter()


def _parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _page_url(
    target: int,
    *,
    q: str,
    module: str,
    start: str,
    end: str,
    page_size: int,
) -> str:
    """拼翻页链接：空值不带进 URL，筛选条件随链接一起走，保证可分享、可回退。"""
    params = {
        "q": q,
        "module": module,
        "start": start,
        "end": end,
        "page": target,
        "page_size": page_size,
    }
    return "/search?" + urlencode({key: value for key, value in params.items() if value != ""})


@router.get("/search")
def search_page(
    request: Request,
    q: str = "",
    module: str = "",
    start: str = "",
    end: str = "",
    page: str = "1",
    page_size: str = "",
) -> Response:
    """关键词 + 时间范围 + module + 分页；任何参数非法都降级，不报错。"""
    cfg = config.get()
    keyword = q.strip()
    module_key = module.strip() if module.strip() in cfg.modules else ""

    start_day = _parse_day(start)
    end_day = _parse_day(end)
    start_utc = repository.day_bounds_utc(cfg.app.tz, start_day)[0] if start_day else None
    end_utc = repository.day_bounds_utc(cfg.app.tz, end_day)[1] if end_day else None

    size = min(
        max(1, _parse_int(page_size, repository.SEARCH_PAGE_SIZE)),
        repository.SEARCH_MAX_PAGE_SIZE,
    )
    current = max(1, _parse_int(page, 1))

    items, total = repository.search_items(
        keyword or None, module_key or None, start_utc, end_utc, current, size
    )
    total_pages = (total + size - 1) // size
    if total_pages and current > total_pages:  # 页码超出（改过 URL / 筛选变窄）→ 落到最后一页
        current = total_pages
        items, _ = repository.search_items(
            keyword or None, module_key or None, start_utc, end_utc, current, size
        )

    url_args = {
        "q": keyword,
        "module": module_key,
        "start": start_day.isoformat() if start_day else "",
        "end": end_day.isoformat() if end_day else "",
        "page_size": size,
    }
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "request": request,
            "page_title": "历史搜索",
            "q": keyword,
            **url_args,
            "page": current,
            "total": total,
            "total_pages": total_pages,
            "items": items,
            "module_options": [(key, cfg.modules[key].label) for key in cfg.modules],
            # 任一高级筛选生效时直接展开，避免「筛选了却看不到自己在筛什么」
            "advanced_active": bool(module_key or start_day or end_day),
            "prev_url": _page_url(current - 1, **url_args) if current > 1 else None,
            "next_url": _page_url(current + 1, **url_args) if current < total_pages else None,
        },
    )

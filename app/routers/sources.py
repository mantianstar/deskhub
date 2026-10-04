"""源管理（plan §10）：列表 + 启停 + 单源立即重试。

写操作只有两件事：启停改 `sources.enabled`，立即重试复用抓取管道的单源入口
（`fetcher.run_once(source_id=...)`），不另写一套抓取逻辑。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app import config, fetcher, repository
from app.models import FetchRun
from app.routers import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _failure_tip(run: FetchRun | None) -> str:
    """标红格的 tooltip：最后一次 `error` 与 `http_status`（plan §10）。"""
    if run is None:
        return "还没有抓取记录"
    parts = [f"最近一次 {run.started_at}：{run.status}"]
    parts.append(f"HTTP {run.http_status}" if run.http_status is not None else "无 HTTP 状态")
    if run.error:
        parts.append(run.error)
    return " / ".join(parts)


@router.get("/sources")
def sources_page(request: Request) -> Response:
    """源列表：入口、启停、最近成功、失败次数；连续失败 >= 3 次标红。"""
    threshold = repository.FAIL_COUNT_RED_THRESHOLD
    last_runs = repository.latest_fetch_runs()
    rows = [
        {
            "id": source.id,
            "module": source.module,
            "name": source.name,
            "url": source.url,
            "enabled": source.enabled,
            "last_ok_at": source.last_ok_at,
            "fail_count": source.fail_count,
            "is_failing": source.fail_count >= threshold,
            "tip": _failure_tip(last_runs.get(source.id)),
        }
        for source in repository.list_sources()
    ]
    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "request": request,
            "page_title": "源管理",
            "rows": rows,
            "fail_threshold": threshold,
            "failing_count": sum(1 for row in rows if row["is_failing"]),
        },
    )


@router.post("/sources/{source_id}/toggle")
def toggle_source(source_id: int) -> RedirectResponse:
    """启停取反，302 回 `/sources`；源不存在返回 404。"""
    enabled = repository.toggle_source_enabled(source_id)
    if enabled is None:
        raise HTTPException(status_code=404, detail=f"源不存在：id={source_id}")
    logger.info("源启停切换 source_id=%s enabled=%s", source_id, enabled)
    return RedirectResponse("/sources", status_code=302)


@router.post("/sources/{source_id}/fetch")
async def fetch_source(source_id: int) -> RedirectResponse:
    """立即抓单个源（忽略启停，供调试与自愈），302 回 `/sources`；源不存在返回 404。"""
    cfg = config.get()
    try:
        # 单源抓取走完正常的落库与状态更新：成功即 fail_count 归零、红点消失
        outcomes = await fetcher.run_once(cfg, source_id=source_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"源不存在：id={source_id}") from None
    outcome = outcomes[0]
    logger.info(
        "单源重试 source=%s status=%s 新增=%d%s",
        outcome.source_name,
        outcome.status,
        outcome.items_new,
        f" error={outcome.error}" if outcome.error else "",
    )
    return RedirectResponse("/sources", status_code=302)

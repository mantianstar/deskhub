"""运行控制（plan §19）：五个功能的启停 + 立即运行。

写操作两件，都不另写一套业务逻辑：

- **启停**：改 `feature_switches`（存库，重启保留），只影响每天 08:00 的定时任务。
- **立即运行**：复用 `app/tasks.py` 的执行入口，后台跑一次，**忽略开关**。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app import config, switches, tasks
from app.routers import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _ensure_known(key: str) -> None:
    if not switches.is_known(key):
        raise HTTPException(status_code=404, detail=f"功能不存在：{key}")


@router.get("/control")
def control_page(request: Request) -> Response:
    """四个功能的开关、运行状态与最近一次结果。"""
    rows = switches.snapshot()
    for row in rows:
        key = str(row["key"])
        row["running"] = tasks.is_running(key)
        row["last"] = tasks.last_state(key)
    return templates.TemplateResponse(
        request,
        "control.html",
        {
            "request": request,
            "page_title": "运行控制",
            "rows": rows,
            "running_count": sum(1 for row in rows if row["running"]),
        },
    )


@router.post("/control/{key}/toggle")
def toggle_feature(key: str) -> RedirectResponse:
    """启停取反，302 回 `/control`；功能不存在返回 404。"""
    _ensure_known(key)
    enabled = switches.toggle(key)
    logger.info("功能开关切换：%s enabled=%s", key, enabled)
    return RedirectResponse("/control", status_code=302)


@router.post("/control/{key}/run")
async def run_feature(key: str) -> RedirectResponse:
    """立即跑一次（忽略开关），交给后台执行后立刻 302 回 `/control`。

    必须是 `async def`：`tasks.trigger` 里要 `create_task`，同步路由跑在线程池里没有事件循环。
    """
    _ensure_known(key)
    if not tasks.trigger(config.get(), key):
        logger.info("该功能正在运行，忽略重复触发：%s", key)
    return RedirectResponse("/control", status_code=302)

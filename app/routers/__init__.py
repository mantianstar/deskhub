"""HTTP 路由。只做参数校验 + 调 repository + 渲染模板。

这里集中一份 Jinja2 环境（`templates`）与时间显示过滤器：digest / search / sources
三个路由共用同一套模板与视觉，避免每个路由各建一份环境导致过滤器漂移。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app import config, repository

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def format_local_time(value: str | None) -> str:
    """UTC 存储串 → 业务时区的 `MM-DD HH:MM`；无值（如美团 feed 没有发布时间）显示 `—`。"""
    parsed = repository.parse_dt(value)
    if parsed is None:
        return "—"
    return parsed.astimezone(config.get().app.tz).strftime("%m-%d %H:%M")


def module_label(key: str) -> str:
    """module key → 中文标签（`config.yaml` 的 `label` 是唯一真源，别在模板里写死）。"""
    return config.get().module(key).label


templates.env.filters["local_time"] = format_local_time
templates.env.filters["module_label"] = module_label

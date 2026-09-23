"""FastAPI 入口：只做 app 装配、lifespan、路由注册、异常处理器。

启动顺序（plan §11.1）：
    日志就位 → 配置加载校验 → 建库 → 同步源清单 → 调度器 → 启动补拉
调度器与启动补拉分别在 M6 / M3 接回，此处预留挂载点。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config, db, logging_setup, repository
from app.routers import STATIC_DIR, digest

logger = logging.getLogger(__name__)


def _bootstrap() -> None:
    """启动阶段：任一步失败都不允许带病启动。"""
    # 文件日志路径本身来自配置，所以先用控制台日志保证错误可见
    logging.basicConfig(
        level=logging.INFO,
        format=logging_setup.LOG_FORMAT,
        datefmt=logging_setup.DATE_FORMAT,
    )

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        logger.error("配置校验失败，进程退出：%s", exc)
        raise SystemExit(1) from exc

    logging_setup.setup(cfg.app.log_path)
    logger.info(
        "deskhub 启动：host=%s port=%s timezone=%s", cfg.app.host, cfg.app.port, cfg.app.timezone
    )
    for message in config.warnings_for(cfg):
        logger.warning(message)

    db.init_db(cfg.app.db_path)
    logger.info("数据库就绪：%s", cfg.app.db_path)

    repository.sync_sources_from_config(cfg.sources)
    logger.info("源清单已同步：%d 个源", len(cfg.sources))

    # M6：scheduler.start()（含启动补拉 create_task(pipeline.run())，不 await 不阻塞首页）


@asynccontextmanager
async def lifespan(_: FastAPI):
    _bootstrap()
    try:
        yield
    finally:
        logger.info("deskhub 已停止")


app = FastAPI(title="deskhub", lifespan=lifespan, docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(digest.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常：%s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    """本地长期运行的自检最小集。"""
    if not db.health_check():
        return {"db": "error", "sources_total": None, "sources_failing": None, "last_fetch_at": None}
    return {
        "db": "ok",
        "sources_total": repository.count_sources(),
        "sources_failing": repository.count_failing_sources(),
        "last_fetch_at": repository.last_fetch_at(),
    }

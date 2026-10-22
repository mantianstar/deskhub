"""SQLite 连接与建表。

单用户单进程，但调度任务与 HTTP 请求共享事件循环，因此**按操作取连接**，不维护全局长连接。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app import migrations, models

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5000

# 允许「向前直接抬版本号」的升级：这些升级只**新增表/索引**，DDL 里是 `CREATE ... IF NOT EXISTS`，
# `executescript(models.DDL)` 已经把它们建好了，所以不需要额外迁移，只把版本号抬上去即可。
# 加列 / 改列 / 改约束**不在其列** —— 那种必须写 app/migrations/vN.py 手工执行（plan §6.2）。
FORWARD_ADDITIVE_UPGRADES = {
    ("1", "2"),
    ("2", "3"),
    ("4", "5"),
    ("5", "6"),
    ("6", "7"),
    ("8", "9"),
    ("9", "10"),
    ("11", "12"),
    ("14", "15"),
}

_db_path: Path | None = None


class SchemaVersionError(Exception):
    """库文件 schema 版本与代码不一致。"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """取一个连接。写操作请自行包在 `with conn:` 中保证事务原子性。"""
    if _db_path is None:
        raise RuntimeError("数据库尚未初始化，请先调用 db.init_db()")

    conn = sqlite3.connect(_db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    try:
        # PRAGMA 是连接级设置，每次连接都要重新打开
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = %d" % BUSY_TIMEOUT_MS)
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    """建表 + 校验 schema 版本。可重复调用（幂等）。"""
    global _db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _db_path = db_path

    with connect() as conn:
        conn.executescript(models.DDL)
        _check_schema_version(conn)


def _check_schema_version(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        with conn:
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (models.SCHEMA_VERSION,),
            )
        return

    current = row["value"]
    if current == models.SCHEMA_VERSION:
        return

    # 只新增表的向前升级：DDL 已由 init_db 里的 executescript 建好，这里仅抬版本号
    if (current, models.SCHEMA_VERSION) in FORWARD_ADDITIVE_UPGRADES:
        with conn:
            conn.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (models.SCHEMA_VERSION,),
            )
            backfill = migrations.DATA_MIGRATIONS.get((current, models.SCHEMA_VERSION))
            if backfill is not None:
                backfill(conn)
        logger.info("schema 版本向前升级：%s → %s（仅新增表，DDL 已建）", current, models.SCHEMA_VERSION)
        return

    # 需要「给老表加列」的升级：DDL 建不了老表的列，走登记好的幂等 ALTER（app/migrations/）
    columns = migrations.COLUMN_MIGRATIONS.get((current, models.SCHEMA_VERSION))
    if columns is not None:
        with conn:
            applied = migrations.apply_columns(conn, columns)
            backfill = migrations.DATA_MIGRATIONS.get((current, models.SCHEMA_VERSION))
            if backfill is not None:
                backfill(conn)
            conn.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (models.SCHEMA_VERSION,),
            )
        logger.info(
            "schema 版本向前升级：%s → %s（加列：%s）",
            current,
            models.SCHEMA_VERSION,
            "、".join(applied) or "无（列已存在）",
        )
        return

    raise SchemaVersionError(
        f"库文件 schema_version={current}，代码要求 {models.SCHEMA_VERSION}；"
        "本版本不做自动迁移，请手工处理（见 plan §6.2）"
    )


def health_check() -> bool:
    """数据库可达性检查。"""
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except (RuntimeError, sqlite3.Error):
        return False
    return True

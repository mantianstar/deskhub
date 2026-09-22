"""SQLite 连接与建表。

单用户单进程，但调度任务与 HTTP 请求共享事件循环，因此**按操作取连接**，不维护全局长连接。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app import models

BUSY_TIMEOUT_MS = 5000

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
    if current != models.SCHEMA_VERSION:
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

"""按版本登记的迁移（plan §6.2）。

`db.init_db` 在建表后调用这里：`CREATE TABLE IF NOT EXISTS` 只能**新增表**，
给老表**加列**（SQLite 不支持 ADD COLUMN IF NOT EXISTS）必须走 ALTER —— 就是本包登记的迁移。

登记表按 (from_version, to_version) 索引；应用是**幂等**的（列已存在则跳过），
因此重复执行安全，也不会与新库的 DDL（已含新列）冲突。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from app.migrations import v4, v8, v11, v12

# (from_version, to_version) -> {表名: {列名: 列定义}}
COLUMN_MIGRATIONS: dict[tuple[str, str], dict[str, dict[str, str]]] = {
    ("3", "4"): v4.COLUMNS,
    ("7", "8"): v8.COLUMNS,
    ("10", "11"): v11.COLUMNS,
}

# (from_version, to_version) -> 建表之外还要跑一次的数据回填
DATA_MIGRATIONS: dict[tuple[str, str], Callable[[sqlite3.Connection], None]] = {
    ("11", "12"): v12.rebuild_coin_lot_fts,
}


def apply_columns(conn: sqlite3.Connection, tables: dict[str, dict[str, str]]) -> list[str]:
    """给老表补列（已存在的列跳过）。返回实际新增的 `表.列` 列表。"""
    applied: list[str] = []
    for table, columns in tables.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            applied.append(f"{table}.{name}")
    return applied

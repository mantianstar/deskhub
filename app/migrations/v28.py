"""v28：纪念币主题 / 名称从收藏系列下推到成员。

一个收藏系列可以含多个成员（如 亚运会 下的 女排 / 男排），主题 / 名称只对成员有意义，
品相匹配与推荐都必须按成员区分。旧数据里系列级的值按成员逐个复制一份，随后删掉系列上的旧列。
"""

from __future__ import annotations

import sqlite3

COLUMNS = {
    "coin_collection_series_members": {
        "theme": "TEXT NOT NULL DEFAULT ''",
        "coin_name": "TEXT NOT NULL DEFAULT ''",
    },
}


def push_down(conn: sqlite3.Connection) -> None:
    """把系列上的主题 / 名称复制给它的每个成员，再删除系列级旧列（幂等）。"""
    series_columns = {row[1] for row in conn.execute("PRAGMA table_info(coin_collection_series)")}
    for column in ("theme", "coin_name"):
        if column not in series_columns:
            continue
        conn.execute(
            f"""
            UPDATE coin_collection_series_members
               SET {column} = COALESCE(
                       (SELECT s.{column} FROM coin_collection_series s
                         WHERE s.id = coin_collection_series_members.collection_id),
                       ''
                   )
             WHERE {column} = ''
            """
        )
        conn.execute(f"ALTER TABLE coin_collection_series DROP COLUMN {column}")


def upgrade_from_26(conn: sqlite3.Connection) -> None:
    """v26 直升级：先补 v27 的品相键迁移，再下推主题 / 名称。"""
    from app.migrations import v27

    v27.upgrade_from_26(conn)
    push_down(conn)

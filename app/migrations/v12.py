"""v11 → v12：为存量拍品回填 FTS 搜索索引。"""

from __future__ import annotations

import json
import sqlite3

from app import textmatch


def rebuild_coin_lot_fts(conn: sqlite3.Connection) -> None:
    """按 `coin_lots` 重建索引（幂等：先清空再插入）。"""
    conn.execute("DELETE FROM coin_lot_fts")
    rows = conn.execute("SELECT id, title, tags FROM coin_lots").fetchall()
    payload = []
    for row in rows:
        tags = json.loads(row["tags"] or "{}")
        payload.append(
            (
                row["id"],
                textmatch.fts_text(
                    row["title"],
                    *(str(key) for key in tags),
                    *(str(value) for value in tags.values()),
                ),
            )
        )
    conn.executemany(
        "INSERT INTO coin_lot_fts(rowid, search_text) VALUES (?, ?)", payload
    )

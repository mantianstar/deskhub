"""v21 → v22：拍品加「是否评级盒」布尔列（`is_graded`）。

站点没有该字段，只能按标题正则判定（plan §18.19.6）；采集时写，历史数据用**纯本地**回填：
`backfill_is_graded` 直接读 `coin_lots.title` 重算，不请求站点。
"""

from __future__ import annotations

import sqlite3

from app.coins.graded import is_graded_title

COLUMNS = {
    "coin_lots": {
        "is_graded": "INTEGER",
    }
}


def backfill_is_graded(conn: sqlite3.Connection) -> None:
    """纯本地回填：按标题重算全部拍品的 `is_graded`（幂等）。"""
    rows = conn.execute("SELECT id, title FROM coin_lots").fetchall()
    conn.executemany(
        "UPDATE coin_lots SET is_graded = ? WHERE id = ?",
        [(is_graded_title(row["title"]), row["id"]) for row in rows],
    )

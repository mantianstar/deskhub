"""v40 → v41：`coin_lot_grades.status` 的「初筛」改名 `prescreen`。

其余状态（pending / graded / failed）本就是英文，改名后口径统一；
只回填存量数据，不加列也不改约束。
"""

from __future__ import annotations

import sqlite3


def rename_prescreen_status(conn: sqlite3.Connection) -> None:
    """把存量 `status='初筛'` 改成 `prescreen`（无匹配行为空操作）。"""
    conn.execute(
        "UPDATE coin_lot_grades SET status = 'prescreen' WHERE status = '初筛'"
    )

"""v31 → v32：把 `coin_purchase_grades` 的历史归一值按当前口径重算。

品相表单落库前已经过 `grades.normalize_key`，但旧口径（未做 `圆 → 元`、
`1/2 → 半`、分数斜杠统一等）留下的行仍是旧写法（如 `1/12 先令`）。
这里按当前函数重算五列，幂等；撞唯一索引的行（同组合已有新写法）保留原样，
不覆盖、不删除，品相记录不丢。
"""

from __future__ import annotations

import sqlite3

from app.coins import grades


def normalize_grade_keys(conn: sqlite3.Connection) -> None:
    """重算国家 / 年份 / 面值 / 主题 / 币名（幂等；撞唯一键的行跳过）。"""
    rows = conn.execute(
        "SELECT id, country, era, denomination, theme, coin_name FROM coin_purchase_grades"
    ).fetchall()
    for row in rows:
        key = grades.normalize_key(
            row["country"], row["era"], row["denomination"], row["theme"], row["coin_name"]
        )
        current = (
            row["country"],
            row["era"],
            row["denomination"],
            row["theme"],
            row["coin_name"],
        )
        if key == current:
            continue
        conn.execute(
            "UPDATE OR IGNORE coin_purchase_grades "
            "SET country = ?, era = ?, denomination = ?, theme = ?, coin_name = ? WHERE id = ?",
            (*key, row["id"]),
        )

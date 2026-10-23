"""v19 → v20：日本品相记录的年份从「年号数字」换成公历年。

品相页与画像改为按公历年关联（`明治6年` = 1873、`大正6年` = 1917），旧记录里的 `era` 只有
年号数字（如 `15`），无法区分年号。这里借历史订单的原始写法（`明治十五年` / `大正十五年`）
反查出唯一的公历年后就地替换；找不到或存在歧义的保持原样。
"""

from __future__ import annotations

import sqlite3

from app.coins import grades


def _is_gregorian(text: str) -> bool:
    return len(text) == 4 and text.isdigit()


def upgrade_japanese_grade_years(conn: sqlite3.Connection) -> None:
    """把日本品相记录的年号数字换成公历年（幂等：已是公历年 / 无法唯一反查的跳过）。"""
    by_legacy: dict[tuple[str, str, str], set[int]] = {}
    for row in conn.execute("SELECT country, era, denomination FROM coin_orders"):
        gregorian = grades.normalize_year(row["era"])
        if not _is_gregorian(gregorian):
            continue
        key = (
            grades.normalize_country(row["country"]),
            grades.year_number(row["era"]),
            grades.normalize_denomination(row["denomination"]),
        )
        by_legacy.setdefault(key, set()).add(int(gregorian))

    for row in conn.execute(
        "SELECT id, country, era, denomination FROM coin_purchase_grades"
    ).fetchall():
        year = grades.normalize_year(row["era"])
        if _is_gregorian(year):
            continue
        key = (
            grades.normalize_country(row["country"]),
            grades.year_number(row["era"]),
            grades.normalize_denomination(row["denomination"]),
        )
        candidates = by_legacy.get(key)
        if not candidates or len(candidates) != 1:
            continue
        conn.execute(
            "UPDATE OR IGNORE coin_purchase_grades SET era = ? WHERE id = ?",
            (str(next(iter(candidates))), row["id"]),
        )

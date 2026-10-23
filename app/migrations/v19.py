"""v18 → v19：把已采集中文表里的国家 / 面值写法与币库展示口径统一。

- 发行方 `India - British` 的旧译文是「印度 - British」，统一成「印度」；
- 面值「四分之一 安那」统一成「1⁄4 安娜」（与品相页 `grades.normalize_denomination` 一致）。
"""

from __future__ import annotations

import sqlite3


def normalize_series_labels(conn: sqlite3.Connection) -> None:
    """修正 `coin_series_cn` 的国家 / 面值（幂等）。"""
    conn.execute("UPDATE coin_series_cn SET issuer = '印度' WHERE issuer = '印度 - British'")
    conn.execute(
        "UPDATE coin_series_cn "
        "SET denomination = REPLACE(REPLACE(denomination, '四分之一 ', '1⁄4 '), '安那', '安娜') "
        "WHERE denomination LIKE '%安那%'"
    )

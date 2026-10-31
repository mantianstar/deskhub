"""v27：品相表与收藏系列增加「纪念币主题 / 名称」两列，品相唯一键扩为 5 列。

旧行的 theme / coin_name 统一置空串，不按「国家 + 面值」推断：同年同面值可能对应多个
主题，自动推断必错（主题 / 名称只由人工维护，见 spec §14.16 / §14.17）。
"""

from __future__ import annotations

import sqlite3

from app.migrations import v26

COLUMNS = {
    "coin_purchase_grades": {
        "theme": "TEXT NOT NULL DEFAULT ''",
        "coin_name": "TEXT NOT NULL DEFAULT ''",
    },
    "coin_collection_series": {
        "theme": "TEXT",
        "coin_name": "TEXT",
    },
}


def rebuild_grade_index(conn: sqlite3.Connection) -> None:
    """把品相唯一键从 3 列换成 5 列，并把两表旧行的主题 / 名称归一为空串（幂等）。"""
    conn.execute("DROP INDEX IF EXISTS uidx_coin_purchase_grades")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uidx_coin_purchase_grades
          ON coin_purchase_grades(country, era, denomination, theme, coin_name)
        """
    )
    for table in ("coin_purchase_grades", "coin_collection_series"):
        conn.execute(
            f"UPDATE {table} SET theme = COALESCE(theme, ''), coin_name = COALESCE(coin_name, '')"
        )


def upgrade_from_20(conn: sqlite3.Connection) -> None:
    """v20 直升级：补跑 v26 的清洗，再重建本版唯一索引。"""
    v26.upgrade_from_20(conn)
    rebuild_grade_index(conn)


def upgrade_from_21(conn: sqlite3.Connection) -> None:
    """v21 直升级：补跑 v26 的清洗，再重建本版唯一索引。"""
    v26.upgrade_from_21(conn)
    rebuild_grade_index(conn)


def upgrade_from_22(conn: sqlite3.Connection) -> None:
    """v22~v25 直升级：补跑 v26 的国家 / 中国清洗，再重建本版唯一索引。"""
    v26.cleanup_china_and_countries(conn)
    rebuild_grade_index(conn)


def upgrade_from_26(conn: sqlite3.Connection) -> None:
    """v26 直升级：只重建索引与归一空值。"""
    rebuild_grade_index(conn)

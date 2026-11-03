"""v29 → v30：所有时间列从 UTC 存储改为北京时间存储。

旧值形如 `2026-09-21T03:12:00Z`，新值形如 `2026-09-21T11:12:00`（纯墙上时间、无后缀）。
`GLOB` 只命中合法旧格式，因此重复执行无副作用；非法值原样保留并计入日志。
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

TIME_COLUMNS: dict[str, tuple[str, ...]] = {
    "sources": ("last_ok_at", "created_at"),
    "items": ("published_at", "fetched_at", "clicked_at"),
    "item_scores": ("scored_at",),
    "fetch_runs": ("started_at", "finished_at"),
    "coin_orders": ("parsed_at",),
    "coin_profile_tags": ("created_at", "updated_at"),
    "coin_shops": ("fetched_at",),
    "coin_sessions": ("auction_at", "fetched_at"),
    "coin_categories": ("fetched_at", "updated_at"),
    "coin_lots": ("closed_at", "fetched_at"),
    "coin_price_refs": ("queried_at",),
    "coin_sales": ("sold_at", "fetched_at"),
    "coin_fetch_runs": ("started_at", "finished_at"),
    "coin_filter_scan_progress": ("updated_at",),
    "coin_series": ("fetched_at",),
    "coin_series_cn": ("fetched_at",),
    "coin_issues": ("fetched_at",),
    "coin_issues_cn": ("fetched_at",),
    "coin_issue_values": ("fetched_at",),
    "coin_api_usage": ("called_at",),
    "coin_purchase_grades": ("updated_at",),
    "coin_collection_series": ("created_at", "updated_at"),
    "coin_collection_series_members": ("created_at",),
    "coin_explore_candidates": ("verified_at", "created_at", "updated_at"),
    "feature_switches": ("updated_at",),
}

_LEGACY_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z"
_NEW_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]"


def to_beijing(conn: sqlite3.Connection) -> None:
    """把旧 UTC 串整体 +8 小时并去掉 `Z`（幂等）。"""
    converted = 0
    skipped = 0
    for table, columns in TIME_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column not in existing:
                continue
            cursor = conn.execute(
                f"UPDATE {table} SET {column} ="
                f" strftime('%Y-%m-%dT%H:%M:%S', substr({column}, 1, 19), '+8 hours')"
                f" WHERE {column} GLOB ?",
                (_LEGACY_GLOB,),
            )
            converted += max(cursor.rowcount, 0)
            skipped += int(
                conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                    f" WHERE {column} IS NOT NULL AND {column} NOT GLOB ?",
                    (_NEW_GLOB,),
                ).fetchone()["n"]
            )
    logger.info("v30 时间列转北京时间：转换 %s 行，非法值保留 %s 行", converted, skipped)

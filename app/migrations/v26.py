"""v26：系列增加「所属系列 / 纪念主题」两列；国家去限定语，删除中国的系列与候选。

图片缓存（`data/coin_images/`）不在这里处理：记录删掉，已下载的图片文件保留。
"""

from __future__ import annotations

import json
import sqlite3

from app.coins import grades
from app.migrations import v22

COLUMNS = {
    "coin_series": {
        "series": "TEXT",
        "commemorated_topic": "TEXT",
    },
    "coin_series_cn": {
        "series": "TEXT",
        "commemorated_topic": "TEXT",
    },
}


def cleanup_china_and_countries(conn: sqlite3.Connection) -> None:
    """存量国家统一去逗号限定语 / 括号注记；系列与候选池里中国的记录删掉。"""
    for table in ("coin_series", "coin_series_cn"):
        for row in conn.execute(f"SELECT numista_id, issuer FROM {table}").fetchall():
            conn.execute(
                f"UPDATE {table} SET issuer = ? WHERE numista_id = ?",
                (grades.normalize_country(row["issuer"]), row["numista_id"]),
            )
        china = [
            row["numista_id"]
            for row in conn.execute(f"SELECT numista_id, issuer FROM {table}").fetchall()
            if grades.is_china_country(row["issuer"])
        ]
        if not china:
            continue
        marks = ", ".join("?" * len(china))
        conn.execute(f"DELETE FROM coin_issue_values WHERE series_numista_id IN ({marks})", china)
        conn.execute(
            "DELETE FROM coin_issues_cn WHERE series_id IN "
            f"(SELECT id FROM coin_series_cn WHERE numista_id IN ({marks}))",
            china,
        )
        conn.execute(
            "DELETE FROM coin_issues WHERE series_id IN "
            f"(SELECT id FROM coin_series WHERE numista_id IN ({marks}))",
            china,
        )
        conn.execute(f"DELETE FROM {table} WHERE numista_id IN ({marks})", china)

    for row in conn.execute(
        "SELECT id, payload, payload_cn FROM coin_explore_candidates"
    ).fetchall():
        payloads: list[str] = []
        china = False
        for raw in (row["payload"], row["payload_cn"]):
            data = json.loads(raw or "{}")
            issuer = grades.normalize_country(data.get("issuer"))
            data["issuer"] = issuer
            china = china or grades.is_china_country(issuer)
            payloads.append(json.dumps(data, ensure_ascii=False))
        if china:
            conn.execute("DELETE FROM coin_explore_candidates WHERE id = ?", (row["id"],))
        else:
            conn.execute(
                "UPDATE coin_explore_candidates SET payload = ?, payload_cn = ? WHERE id = ?",
                (*payloads, row["id"]),
            )


def upgrade_from_20(conn: sqlite3.Connection) -> None:
    """v20 直升级：补跑 v22 的 is_graded 回填，再做本版清洗。"""
    v22.backfill_is_graded(conn)
    cleanup_china_and_countries(conn)


def upgrade_from_21(conn: sqlite3.Connection) -> None:
    """v21 直升级：补跑 v22 的 is_graded 回填，再做本版清洗。"""
    v22.backfill_is_graded(conn)
    cleanup_china_and_countries(conn)

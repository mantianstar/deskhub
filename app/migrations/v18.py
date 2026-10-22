"""v17 → v18：给拍品补热度字段与同图识别用的 OSS key（plan/coins.md §18.18.1 P1-6 / P1-7）。

- `bid_num` / `bid_user_num` 只从 `ListAuction` 预览与 `home.filter` 拿；
  `auction.get` 没有这两个键，缺就是 NULL。`bid_source` 记来源，便于按来源统计非空率。
- `coin_lot_images` 是新表，由 `models.DDL` 直接建；老数据从 `coin_lots.image_urls`
  回填 OSS key（去 base、去 query），否则重复识别只能看到新采集的拍品。
"""

from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlsplit

COLUMNS = {
    "coin_lots": {
        "bid_num": "INTEGER",
        "bid_user_num": "INTEGER",
        "bid_source": "TEXT",
    }
}


def _oss_key(raw: object) -> str | None:
    """图片地址 → OSS key（与 `app/coins/gapi._oss_key` 同一口径）。"""
    if not isinstance(raw, str):
        return None
    key = raw.split("#", 1)[0].split("?", 1)[0].strip()
    if "://" in key:
        key = urlsplit(key).path.lstrip("/")
    return key or None


def backfill_coin_lot_images(conn: sqlite3.Connection) -> None:
    """把已有的 `coin_lots.image_urls` 转成 `coin_lot_images` 行（幂等）。"""
    rows = conn.execute(
        "SELECT id, image_urls FROM coin_lots WHERE image_urls IS NOT NULL"
    ).fetchall()
    payload: list[tuple[int, str]] = []
    for row in rows:
        try:
            urls = json.loads(row["image_urls"] or "[]")
        except ValueError:
            continue
        if not isinstance(urls, list):
            continue
        seen: list[str] = []
        for url in urls:
            key = _oss_key(url)
            if key and key not in seen:
                seen.append(key)
                payload.append((int(row["id"]), key))
    if payload:
        conn.executemany(
            "INSERT OR IGNORE INTO coin_lot_images (lot_id, oss_key) VALUES (?, ?)",
            payload,
        )

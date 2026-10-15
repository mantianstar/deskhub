"""v3 → v4：`coin_series` 加 `comments` / `mints`，`coin_issues` 加 `catalog_no`。

同版本新增的 `coin_series_cn` / `coin_issues_cn` / `coin_api_usage` 是**纯新增表**，
由 `models.DDL` 的 `CREATE ... IF NOT EXISTS` 建好，这里只负责给**老表加列**。
"""

from __future__ import annotations

COLUMNS: dict[str, dict[str, str]] = {
    "coin_series": {"comments": "TEXT", "mints": "TEXT"},
    "coin_issues": {"catalog_no": "TEXT"},
}

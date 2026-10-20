"""v10 → v11：`coin_shops` 加 `followed`（关注店铺）。

关注是本地标记，采集时的 upsert 不覆盖该列。
"""

from __future__ import annotations

COLUMNS: dict[str, dict[str, str]] = {
    "coin_shops": {"followed": "INTEGER NOT NULL DEFAULT 0"},
}

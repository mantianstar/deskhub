"""v16 → v17：给专场补费率、运费与寄存（plan/coins.md §18.18.1 P1-8）。

`auction.get` 直取 `commission_rate` / `commission_min` / `express_desc` / `express_day`；
`express_fee` / `express_free_amount` / `is_storage` 由 `app/coins/gapi.py` 从运费说明文本里抽基础值。
"""

from __future__ import annotations

COLUMNS = {
    "coin_sessions": {
        "commission_rate": "REAL",
        "commission_min": "REAL",
        "express_desc": "TEXT",
        "express_day": "INTEGER",
        "express_fee": "REAL",
        "express_free_amount": "REAL",
        "is_storage": "INTEGER NOT NULL DEFAULT 0",
    }
}

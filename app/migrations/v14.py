"""v13 → v14：给拍品补终态（成交/未成交、成交价、中拍人、终态时间）。

对应 spec/coins.md §14.18 / plan/coins.md §18.18：已结束专场的 `auction.get` 每次就带回全部拍品的
`status` / `price` / `won_user_id` / `end_time`，回采即得。
"""

from __future__ import annotations

COLUMNS = {
    "coin_lots": {
        "outcome": "TEXT",
        "final_price": "REAL",
        "winner_uid": "TEXT",
        "closed_at": "TEXT",
    }
}

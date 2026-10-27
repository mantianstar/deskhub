"""v23 → v24：`coin_lots` 补 `home.filter` 带来的热度明细。

`bid_users`（出价人 uid 数组）与 `winner_nickname`（中拍人昵称）只由 filter 扫描写入；
只加列，不回填 —— 下一次扫描按 `mxiqi_id` 精确更新。
"""

from __future__ import annotations

COLUMNS = {
    "coin_lots": {
        "bid_users": "TEXT",
        "winner_nickname": "TEXT",
    }
}

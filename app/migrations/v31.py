"""v31：给 coin_sessions 加「累计围观人数」列。

值来自 `miniprogram.auction.get` 的 `auction_user_total`（去重 UV），
与已有的 `view_count`（`auction_stat.pv`，次数）并存；历史场次需回采补数。
"""

from __future__ import annotations

COLUMNS = {
    "coin_sessions": {
        "viewer_count": "INTEGER",
    },
}

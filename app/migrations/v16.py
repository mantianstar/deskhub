"""v15 → v16：给拍品补 Numista 币种映射（系列 / 单版 / 匹配方式 / 置信度）。

对应 spec/coins.md §14.18 / plan/coins.md §18.18.1 P0-4 与 §18.18.5：
「按币种聚合」不能靠标题猜，先落一套可靠关联；多候选宁缺勿错。
"""

from __future__ import annotations

COLUMNS = {
    "coin_lots": {
        "series_numista_id": "INTEGER",
        "issue_numista_id": "INTEGER",
        "match_method": "TEXT",
        "confidence": "REAL",
        "match_candidates": "TEXT",
    }
}

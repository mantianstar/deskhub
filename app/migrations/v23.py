"""v22 → v23：分类字典补站点 `cate.list` 的字段。

`miniprogram.cate.list` 现在可用（`data.cates[]` 一级、`sub[]` 二级），
`coin_categories` 从「码 + 名称」扩成层级字典：父码 / 层级 / 缩略图 / 拍品数快照 / 排序。

只加列，不回填：老行的 `parent_code` / `level` 由下一轮采集前的 `categories.sync()` 写正。
"""

from __future__ import annotations

COLUMNS = {
    "coin_categories": {
        "parent_code": "TEXT",
        "level": "INTEGER NOT NULL DEFAULT 1",
        "thumb": "TEXT",
        "item_count": "INTEGER",
        "order_no": "INTEGER",
    }
}

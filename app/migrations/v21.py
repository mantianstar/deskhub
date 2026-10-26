"""v20 → v21：拍品级分类（站点原始码）与分类字典。

分类挂在**拍品级**：`coin_lots.category_code` 存站点原始码，名称统一放 `coin_categories`
字典（采集时 upsert），查询不用再回站点。识别不出的拍品保持 NULL，分析页归入「未分类」。

`coin_categories` 是新表，由 `models.DDL` 直接建；老库这里只补 `coin_lots.category_code`。
"""

from __future__ import annotations

COLUMNS = {
    "coin_lots": {
        "category_code": "TEXT",
    }
}

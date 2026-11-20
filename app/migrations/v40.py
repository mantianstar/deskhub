"""v39 → v40：`coin_lot_grades` 增加初筛三列。

初筛在匹配与特征提取之间：模型返回原文、是否通过（pass_to_next）、初筛时间。
状态值新增 `初筛`（走 status 列，无需改表结构）。
"""

from __future__ import annotations

COLUMNS: dict[str, dict[str, str]] = {
    "coin_lot_grades": {
        "prescreen_raw": "TEXT",
        "prescreen_pass": "INTEGER",
        "prescreen_at": "TEXT",
    },
}

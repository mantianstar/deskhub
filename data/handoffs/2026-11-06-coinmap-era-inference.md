status: pending

# 2026-11-06 映射：标题面值归一 + 日本年号推断

- 已完成：v1.125 标题面值归一（中文数字 / `1⁄2` → `半`，`coinmap._denomination_text`）；v1.126 日本年号推断国家（`_JP_ERA_RE`，明治 / 大正 / 昭和 / 平成 / 令和 → 日本）。`tests/test_coins_coinmap.py` 补 5 条用例，文件内 17 条通过；库内 dry-run 975 条候选里 58 条现在可映射；CHANGELOG 已记 v1.125 / v1.126。
- 未完成：未重跑全量 pytest（v1.125 时全量 547 passed；v1.126 只跑了文件级）；未提交 git；未重跑 `coins-map` 写库。
- 下一步：`\.venv/bin/python -m pytest tests -q` → 确认后重跑映射命令落库那 58 条 → 代码与 CHANGELOG 同笔提交。
- 约束：全量 pytest 只跑一次；国家优先级＝属性 → 标题国家词 → 日本年号推断；民国纪年不得推断为日本。
- 读取起点：`app/coins/coinmap.py` `lot_facts`（≈143-157）、`_denomination_text`（≈99-103）；`tests/test_coins_coinmap.py` 103-173。

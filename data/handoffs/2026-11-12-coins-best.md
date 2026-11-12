status: done（2026-11-12 收尾：升级链补 36→37、版本断言 36→37、提示词断言同步、explore 超限用例改 101 条；全量 615 passed / 1 skipped）
# 铜币「好品推荐」交接（2026-11-12 · 新用例已通过，全量仍有 10 条红）

## 需求原句（不改写）
1、遍历币库，生成国家+年份+面值搜索词，匹配今日拍品里11070类别的拍品数据，匹配到的数据入库
2、对匹配到的数据调大模型得到铜币的品相并入库，包含评级分数、中文品相、铜色等级、磨损描述、铜色描述、瑕疵、盲区、置信区，同时对铜币品相评级做风控，每天最多 50 个，超过 50个的不予评价
3、筛选出品相>=XF40的拍品，展示在今日推荐 精准匹配区块后面，增加一个区块【好品推荐】，卡片样式参考精准匹配，原估值和分数换成评级分数和铜色等级，每个卡片只有一个拍品，右上角标签为【好品推荐】，同时精准匹配、好品推荐、相似推荐 3 个区块都要分页，每页 3 个
4、整个好品推荐放到每日调度里，放在原推荐模块里一起跑

## 已确认口径
- 好品推荐只跟随「收藏系列」筛选；推荐原因 / 命中状态不影响它。
- 评级分数展示模型输出区间原文（如 XF40-45）；阈值/排序用区间下界（40）。
- 跨区块不去重。

## 已完成
- 业务代码：models（SCHEMA_VERSION=37、coin_lot_grades）、db（36→37）、coin_grade/parse.py、coins/best.py、repository 8 个新函数、pipeline 挂载、routers/coins.py 三分页、collection.html 好品区块（详见上一版交接，均未改动）。
- `tests/test_coins_best.py`（新建）：temp_db 建 coin_categories（11070/11060，否则 coin_lots.category_code 外键失败）+ 手工 Config；用例 `test_match_today_lots_matches_and_is_idempotent`（命中 / 年份不命中 / 非 11070 不命中 / 跑两次仍 1 行）、`test_grade_candidates_skips_missing_image_and_caps_daily_quota`（FakeClient 六行文本、无图记 failed 不占额度、load_best_lots 返回 XF40-45/40、monkeypatch count_coin_graded_on_day=50 后为 0）。
- `tests/test_coin_grade.py`：追加 `parse_grade` 完整六行 / 缺行 / 非法分数，`is_best` 前缀与下界；
- `tests/test_coins_collection.py:1441-1446`：25 条推荐 → 第 1 / 9 页、page=2 → 第 2 / 9 页。
- 定向三文件：62 passed，仅剩 1 条历史红（见下）。

## 未完成（全量 `pytest -q`：10 failed / 604 passed / 1 skipped）
1. `tests/test_coin_grade.py::test_system_prompt_keeps_key_constraints`：断言「无法判定」不在新 SYSTEM_PROMPT（上一份 2026-11-11-coin-grade-mimo-prompt 交接已重写提示词，测试未同步）。
2. schema 版本断言红 6 条：`test_coins_analysis.py`（fresh '37'=='36'、v21 回填、v22 幂等、v27、v28）与 `test_coins_categories.py`（fresh、v20 升级、v21 幂等）：SCHEMA_VERSION 36→37 后旧快照/升级链未同步。
3. `tests/test_coins_explore_page.py::test_run_page_explore_over_50_stops_before_write`：断言期望不落库却拿到 1 条 CoinPageExplore（疑似既有红，未定位）。

## 下一步
- 先确认上述 10 条是否为 36→37 / 提示词重写带来的预期红：是则同步断言（v36→v37、升级链补 36→37），不是则续查 explore 用例。
- 全绿后把本文件首行改 `status: done`，回复出「回执」四行。

## 约束
- 不改推荐算法、既有卡片文案/样式；除分页 20/5→3 外不改原行为；不新增依赖；调度文件不动；跨区块不去重。

## 读取起点
- 本文件；`tests/test_coins_best.py`（新）；`app/coins/best.py`（6 符号）；`tests/test_coins_analysis.py` / `tests/test_coins_categories.py` 中版本断言段。

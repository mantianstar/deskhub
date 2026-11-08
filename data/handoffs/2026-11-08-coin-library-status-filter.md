# 2026-11-08 币库列表状态筛选（续跑交接）

status: done（2026-11-08 验证通过：info_page 7 passed；全量 572 passed / 1 skipped）

## 已完成

- `app/routers/coins.py` `info_page`：新增 `status` 参数，先算 `watched_ids` / `collected_ids`，再按状态过滤（collected / watched / unwatched，互斥，已收藏优先），过滤发生在分页前；`filter_params` 带 `status`，翻页保留条件；模板上下文改传已算好的集合 + `status`。
- `app/templates/coins/info.html`：筛选表单新增「状态」下拉（全部 / 已收藏 / 已观察 / 未观察），位于「材质」之后、「查询」之前。
- `tests/test_coins_pages.py`：
  - 新增 `test_info_page_filters_by_status`（670 观察、1867 收藏、857/13222 未观察）。
  - `test_info_page_watch_button_adds_and_is_idempotent` 两条断言由「页面无『已观察』文案」改为检查 `coin-follow-btn is-followed` 类名（下拉选项必然含「已观察」四字，原断言无法共存）。
- `CHANGELOG.md`：新增 v1.144 条目。

## 未完成

- 最后一版 `test_info_page_filters_by_status` 断言尚未跑过验证（改动为 unwatched 断言：`"George III" not in` 且 `"Edward VII" in`）。
- 全量测试最后一次结果是 1 failed / 571 passed，失败即上述用例（断言写错，非业务代码问题，已修正待验）。

## 下一步

1. `.venv/bin/python -m pytest tests/test_coins_pages.py -q -k "info_page"`，确认 7 passed。
2. 通过后 `.venv/bin/python -m pytest -q` 收尾（预期 572 passed, 1 skipped）。
3. 无需再改业务代码；如测试仍失败，只需调 `tests/test_coins_pages.py:769-790` 的断言（注意页面 datalist 会包含全部国家 / 面值 / 材质，不能用作过滤判据）。

## 约束

- 不改既有文案；已改的两条旧断言属必要冲突，回复中需说明。
- 不新增 spec / plan 正文修改。

## 读取起点

- `app/routers/coins.py` `info_page`（约 472-585 行）
- `app/templates/coins/info.html`（45-53 行）
- `tests/test_coins_pages.py`（769-806 行）

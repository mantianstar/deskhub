status: done
# 币库 6 组改动交接


## 已完成（本次会话已落盘）
- `_macros.html`：新增 `coin_series_card_meta(item, year_count, title_href="")`（尾部 58-74 行）。
- `app/routers/coins.py`：
  - `info_page` context 加 `collected_ids`、`year_counts`（548-552）。
  - `watchlist_page` context 加 `year_counts`（651）。
  - `collection_detail_page` 加 `commemorative_ids` / `has_commemorative_member` / `member_year_counts`，并移除 `grade_entries` 与 `grades`（2433-2462）。
- `collection_detail.html`：删品相表单（原 43-65）、删「纪念币成员会自动多出两列」提示与两个模式按钮（原 125-129）、删 `[data-member-mode]` JS；两列改由 `has_commemorative_member` 控制显示；枚数改为 `member_year_counts.get(...) or '—'`；空态 colspan 动态。
- `series.html`：删 43 行 `Numista #… · 目录号` 提示；删「造币厂」；`<details>` 内联编辑改为 ✎ 按钮 + 弹窗（`#issue-edit-scrim` / `#issue-edit-modal`，120 行一带），form 仍 POST `/coins/series/{id}/issue-fields`；文件末尾（content block 内）追加弹窗 JS。
- `watchlist.html`：`<a class="coin-series-main">` → `<div>`，meta 改用宏，标题唯一 `<a>`。
- `info.html`：meta 改用宏；按钮三态 已收藏（disabled，`is-collected`）＞已观察＞加入观察。
- `style.css`：加 `.coin-follow-btn.is-collected`（657）、`.coin-series-title a`（916）。
- `app/cli.py`：新增 `cmd_coins_explore_adopt`（546-557）并注册 `coins-explore-adopt`（749-752）。
- `tests/test_coins_collection.py:1338-1342` 已改为断言无品相面板 + 枚数列。

## 未完成（按顺序）
1. `tests/test_coins_pages.py:801` 仍断言 `"英国 · 1 便士"`，需改为 `"英国 · George III · 1 便士"`（仅 801 行；1164 行是采集页，保持不动）。
2. 跑验收：`.venv/bin/pytest tests/test_coins_pages.py tests/test_coins_collection.py -q`，修到全绿。
3. 执行一次 `.venv/bin/python -m app.cli coins-explore-adopt`（candidate + ignored 全采纳，采纳后候选池清空属预期），确认输出成功/失败数。
4. 手工确认 `/coins/series/{id}` 弹窗可开可关（无 JS 报错）。
5. 完成后把本文件首行改为 `status: done`。

## 约束
- 不碰 `/coins/grades`、个人收藏清单、推荐/画像算法、采集与翻译、路由 URL。
- 枚数 / 年份数统一用去重口径（`coin_series_issue_stats` 的 `year_count`）。
- Read 带 offset/limit ≤120 行；Grep 带符号名。

## 待留意
- `collection_detail.html` 的 `commemorative_ids` 已不再被模板引用（宏/列改用 `has_commemorative_member`），如后续清理可一并删除。
- `series.html` 旧的 `.coin-issue-edit` 样式与 `coin-member-meta` 类已无引用。

## 收尾结果（2026-11-08 续跑）
- 已完成：补齐 `series.html` 外层缺失的 `{% endif %}`（原 340 行前）；测试断言改为「英国 · George III · 1 便士」（`test_coins_pages.py` 758/760/801），删除已下线「造币厂」的 `Royal Mint` 断言（855）；`pytest tests/test_coins_pages.py tests/test_coins_collection.py` → 154 passed；`coins-explore-adopt` → 成功 371 / 失败 0；浏览器实测 `/coins/series/72` 弹窗可开可关、无 JS 报错。
- 未完成：无。
- 下一步：无（可顺带清理 `commemorative_ids` 与旧样式，非必须）。
- 约束：同上。
- 读取起点：本文件。

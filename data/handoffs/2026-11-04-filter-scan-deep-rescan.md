status: done
# filter_scan：深扫到底后按冷却重扫 + 顶部按量估页

- 已完成：`app/coins/filter_scan.py` 三处改动——①断点 `>= DEEP_DONE_PAGE` 且缺口 > 0、距上次推进超过 `DEEP_RESCAN_HOURS=72` 时重置为 1 重扫（并打日志）；②room 对接近封顶仍可续扫的类保底 `MIN_DEEP_PAGES`；③新增 `_keepalive_pages`，顶部保活页按上次成功扫描后的成交数估页。新增 `tests/test_coins_filter_scan.py::test_full_scan_restarts_done_category_with_gap`，`pytest tests/test_coins_filter_scan.py` 13 passed。
- 未完成：无（代码与用例均已完成）。
- 下一步：可选——a) 把 `coin_filter_scan_progress` 中 11070 的 `updated_at` 回拨或 `next_page` 改回 1，立即触发重扫；b) 跑一轮全量，核对 11070 的 `bid_num IS NULL AND bid_source IS NULL` 是否下降。
- 约束：11070 有 14897 条成交，站点 totalSize 封顶 10000（≈476 页），缺口无法归零；重扫一轮约 476 页，故用 72h 冷却，避免天天吃掉日配额（900）。`_reclaim_pages` 未改。
- 读取起点：`app/coins/filter_scan.py` L212-254（新增 helper）、L427-463（顶部估页与 room/断点重置）。

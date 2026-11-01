status: done

# 2026-11-01 home.filter 增量 3 天下限 + 关闭晚间深扫 + 日配额 600

## 已完成
- `app/coins/filter_scan.py`：`DAILY_QUOTA` 900→600；新增 `INCREMENTAL_LOOKBACK_DAYS = 3`；`_scan` 非全量模式新增 `incr_floor`，页内条目整页早于 3 天则 `stopped="incremental_floor"` 并 `break`（只停当前二级分类，其他分类继续）；scan docstring 同步。
- `app/scheduler.py`：移除 `daily_coins_deep` 函数、`DAILY_COINS_DEEP_JOB_ID`、`DEEP_HOUR` 及 21:00 注册；模块 docstring 注明改为手工 `coins-filter-scan --full`。
- 验证：`pytest tests/test_coins_filter_scan.py tests/test_control.py -q` → 19 passed。

## 未完成
- CHANGELOG 尚未加条目（按惯例应为 v1.116，写「增量 3 天下限 + 移除晚间深扫 + 配额 600」）。
- 增量 3 天下限没有专门测试用例（可仿 `test_full_scan_stops_deep_when_pages_older_than_lookback`，非 full 模式、第 2 页给旧 `update_time`，断言 `requested == [1, 2]` 且 `stopped == "incremental_floor"`）。
- 全量 pytest 未跑（本段会话预算已超 1.2M）。

## 下一步
1. 补增量下限用例；
2. 跑 `.venv/bin/python -m pytest tests -q` 全量一次；
3. 追加 CHANGELOG v1.116 条目。

## 收尾结果（2026-11-01 续跑）
- 补 `test_incremental_scan_stops_when_pages_older_than_lookback`（非 full、第 2 页给旧 `update_time`，断言 `requested == [1, 2]`、`stopped == "incremental_floor"`）。
- 全量 pytest 首轮 526 passed / 1 failed：`test_deep_scan_job_fires_at_9pm_local` 仍引用已删的 `DAILY_COINS_DEEP_JOB_ID`，已删除该用例；复跑 `tests/test_scheduler.py tests/test_coins_filter_scan.py` → 19 passed。
- CHANGELOG 已加 v1.116。
- 全量 pytest（删用例后）526 passed, 1 skipped；无遗留。

## 约束
- 只做被要求的；不改既有文案与测试断言；一个完整改动一笔提交。

## 读取起点
- `app/coins/filter_scan.py` L47-L52（常量）、L340-L345（cutoff/incr_floor/deep_floor）、L449-L460（下限判断）
- `app/scheduler.py` L3-L10、L32-L36、L120-L140
- `tests/test_coins_filter_scan.py` L191-L215

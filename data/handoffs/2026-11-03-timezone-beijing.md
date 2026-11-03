status: done

# 时间口径统一：全库存储、代码转换、页面展示全部改为北京时间

## 已完成（本轮，代码已落盘）

1. `app/repository.py`：新增 `BEIJING = ZoneInfo("Asia/Shanghai")`；`utcnow→now_local()`（`YYYY-MM-DDTHH:MM:SS` 无后缀）、`to_utc_str→to_local_str()`（aware → 北京 → 去 tzinfo）、`parse_dt()` 改为返回 naive 北京时间（兼容旧 `Z` 串）。`day_bounds_utc→day_bounds_local`、`local_month_start_utc→local_month_start`，`local_day_bounds` / `lookback_bounds` 同步返回本地串。
2. 全库标识符批量改名（138 处，app + tests）：`utcnow / to_utc_str / day_bounds_utc / local_month_start_utc` 四组；gapi `to_utc_iso→to_local_iso`（5 处）+ filter_scan 引用（2 处）。
3. 新建 `app/migrations/v30.py`：`TIME_COLUMNS`（25 表，已用 DDL 校验无缺列）+ `to_beijing(conn)`，`GLOB` 只命中旧 `...Z` 格式、非法值保留并计数写日志，幂等。
4. `app/models.py`：`SCHEMA_VERSION = "30"`，时间列注释改北京时间。
5. `app/migrations/__init__.py`：import v30；`COLUMN_MIGRATIONS[("29","30")] = {}`（空 dict 仍走加列分支，从而执行回填）；`DATA_MIGRATIONS[("29","30")] = v30.to_beijing`；`20…28 → 30` 直升级用 `_previous = DATA_MIGRATIONS.get(...)` 兜住 `("28","29")` 缺失（**此坑首跑已踩**：直接索引 KeyError）。
6. 语义改动：`gapi.to_local_iso` 直接按业务时区格式化；`fetcher._published_at_from_url` 取本地 00:00（去掉 `tzinfo=utc`）；`routers/__init__.format_local_time` 去掉 `astimezone`；`filter_scan` 的 `_UTC_FORMAT` 去 `Z`、`_parse_utc` 改为 `parse_dt` + 补北京 tzinfo、`_hours_ago_iso` / `_day_start` 走 `to_local_str`、两处 `datetime.now(timezone.utc)` → `now(BEIJING)`；`pipeline` 终态回采 / 分类回填的时间窗改本地串（`1970-01-01T00:00:00`）；`pricing._within_window` cutoff 改 naive 本地；`routers/coins.py` 推荐日改 `moment.date()`；`repository` L1733 purge cutoff 改 `to_local_str`。

## 当前状态

`pytest -q`：**26 failed, 500 passed, 1 skipped**。全部失败都是断言里的旧口径（未改业务逻辑）。

## 下一步（只剩测试断言与文档）

两类失败，按类批量改：

1. 版本断言（7 个，`tests/test_coins_analysis.py` 5、`tests/test_coins_categories.py` 3 中的版本断言）：建库 / 升级后 `schema_version` 期望值 `"29"` → `"30"`。注意 `test_v21/v22/v27/v28/v20_db_upgrade_*` 这类用例只断言升到当前版本，改期望即可；`test_fresh_db_has_is_graded_column`、`test_fresh_db_has_category_schema` 同理。
2. 时间串断言（19 个）：
   - `test_fetcher.py`（7）：URL 兜底日期 `2026-07-04T00:00:00Z → 2026-07-04T00:00:00`；feed 时间从 UTC 改北京（`18:00:00Z → 次日 02:00` 这类整体 +8，如 `test_feed_time_wins_over_url_date` 期望 `2026-09-21T18:00:00`）；`published_tz` 重解释两例仍应等于墙上时间。
   - `test_digest.py`（3）：`day_window_matches_shanghai_midnight` / `day_boundaries_split` 的边界期望改为 `2026-09-22T00:00:00`、`2026-09-23T00:00:00`；`lookback_bounds` 断言改本地串。
   - `test_search.py::test_search_filters_by_published_time_not_fetched_time`：`2026-09-21T16:00:00Z → 2026-09-22T00:00:00`（左右各 +8）。
   - `test_repository.py::test_purge_deletes_older_items_and_cascades_scores`：`2026-09-21T16:00:00Z → 2026-09-22T00:00:00`。
   - `test_coins_fetch.py`（5）：`test_to_utc_iso_*` 改名 `test_to_local_iso_*` 且期望去 `Z`；`session_end_at_in_utc`、`card_from_auction`、`lot_from_item`、`sold_from_payload` 的期望串 +8 去 `Z`。
   - `test_coins_numista.py::test_local_month_start_is_first_day_of_business_month`：期望 `...T00:00:00`（原为 `Z` 或 UTC 偏移）。
3. 绿了之后：`CHANGELOG.md` 记一条；`README.md`、`plan/*.md` 里「UTC」口径原地改；可选：新增一条 v30 迁移单测（旧 `Z` 串 → +8 无后缀、跑两次结果不变）。

## 约束

- 只做需求点名的：不改文案、样式、字段名、算法；不顺手重构。
- 存储串定长 `YYYY-MM-DDTHH:MM:SS`；SQL 只换值不换结构；不重建索引。
- 迁移幂等、与 schema_version 同事务；失败整体回滚。
- 页面不做时区换算。

## 读取起点（续跑）

- `tests/test_fetcher.py` 相关用例（grep `Z"` / `published_at`）
- `tests/test_digest.py::test_day_window_matches_shanghai_midnight`
- `tests/test_coins_fetch.py`（grep `to_utc_iso`）
- `tests/test_coins_analysis.py` `test_v2*`、`tests/test_coins_categories.py` `test_v2*`

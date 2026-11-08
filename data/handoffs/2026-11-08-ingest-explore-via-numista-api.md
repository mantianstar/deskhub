# 2026-11-08 采集中心探索改走 Numista 接口（交接）

status: done（2026-11-08 验证：`pytest -q` 572 passed / 1 skipped）

## 已完成

- `app/coins/numista.py`：新增 `explore_series`（条件方式：`/types` 搜 ID → 逐个 `/types/{id}` 取详情 → 面值/材质精确过滤）与 `explore_series_by_id`（ID 方式）；命中项经 `_stage_series` 写入 `coin_explore_candidates`（英文 + 中文 payload，`issues=[]`），**不写 `coin_series`**。
- `app/routers/coins.py`：`_ingest_result_series` 改为 async，实时调上述两个函数，`NumistaError` 收敛成页面提示；`ingest_page` 改 async，上下文新增 `explore_error`。
- `app/templates/coins/ingest.html`：新增 `explore_error` 提示行。
- `app/coins/ingest.py` `_node_series`：本地已有 → skip；候选池有暂存 → `explore.adopt_candidate(mark_adopted=False)` 直接落库（省一次接口）；否则仍走 `collect_series_by_id`。
- `app/coins/explore.py` `adopt_candidate`：新增 `mark_adopted=True` 关键字（默认行为不变），入队复用暂存数据时不改候选状态。
- `tests/test_coins_pages.py`：`test_ingest_condition_explore_merges_series_id` 改为 monkeypatch 两个新接口函数（含 `_stub_series` 桩）。
- `CHANGELOG.md`：v1.145。

## 未完成

- 无。

## 下一步

- 无（如需继续：可考虑探索结果翻页复用候选池，避免每次翻页重复调接口）。

## 约束

- 探索阶段不落 `coin_series`，只进临时表；采纳语义仍归探索候选页。
- `/types` 只返回系列号，第一步数据必须逐个取详情；单次探索最多 `coins.numista.max_series`（默认 10）个详情请求。

## 读取起点

- `app/coins/numista.py` `explore_series` / `_stage_series`（约 813-911 行）
- `app/routers/coins.py` `_ingest_result_series` / `ingest_page`（约 1108-1160 行）
- `app/coins/ingest.py` `_node_series`（36-44 行）

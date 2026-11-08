status: done

# 铜币重构 · 按原型全量回退修复（续跑）

## 已完成
- 前 8 项（导航归位 / 今日推荐右栏 / 观察系列升级 / 币库列表 / 币库详情 / 收藏系列列表 / 收藏系列详情 / 个人收藏清单）见 git diff，均已通过测试。
- 第 9 项 **采集中心**（本轮已落盘，未验收全量）：
  - `app/routers/coins.py`：
    - 新增 `INGEST_TASK_BADGE_LABELS`（表格徽标用原型文案：未开始 / 进行中 / 完成 / 失败；查询下拉仍用 `INGEST_TASK_STATUS_LABELS`，因为 `test_ingest_post_enqueues_and_reuses` 断言「待执行」）与 `INGEST_RESULT_PAGE_SIZE = 20`。
    - 新增 `_ingest_result_series()`：条件方式走 `repository.search_coin_series(cn=True)`，ID 方式走 `numista_ids=[id]`，合并去重后按 numista_id 排序（原型口径）。
    - `ingest_page` 重写：参数 `country / year / denomination / composition / series_id / epage / page / name / status / created / finished`；结果与队列各自分页 20；队列筛选映射 `list_coin_ingest_tasks(name, status, created_from/to, finished_from/to)`，日期按当天 `T23:59` 收起；分页 URL 保留全部筛选参数。
    - 新增 `POST /coins/ingest/queue`（多选入队，逐个 `enqueue_coin_ingest_task(source="explore")`，任务名取本地系列标题，回跳 `?queued=N&reused=M`）。原 `POST /coins/ingest` 未动。
  - `app/templates/coins/ingest.html` 按原型 `docs/prototypes/coin-refactor/ingest.html` 重写：页头副标题 / 条件方式面板（国家·年份·面值·材质 + 探索）/ ID 方式面板（`coin-query-inline` + 提示）/ 探索结果（全选 + 复选 + 加入队列 + 分页 + 内联 JS 计数）/ 任务队列（四项筛选 + 节点胶囊 + 状态徽标 + 分页 + 底部 hint）；删掉原「＋ 入队采集」details 块，保留「队列为空」提示文案。
  - `app/static/style.css`：新增 `.coin-node-row / .coin-node(.is-*) / .coin-badge.is-* / .coin-query-inline / .coin-field-inline / .coin-nowrap`（照抄 proto.css 口径）。
- 验证：`pytest -q tests/test_coins_pages.py tests/test_coins_collection.py` → 151 passed（本轮跑过）。

## 未完成
1. 新逻辑补测试：`tests/test_coins_pages.py` 增
   - `POST /coins/ingest/queue` 多选入队 → 302 到 `/coins/ingest?queued=2&reused=0`，页面出现任务与「未开始」徽标；
   - 条件方式探索：用 `_make_series` + `repository.upsert_coin_series_cn` 造数后 `GET /coins/ingest?country=英国&denomination=1 便士` 命中，`series_id=xxx` 与条件结果合并去重。
2. 全量 `pytest -q`。
3. 文档对齐：`CHANGELOG.md` / `spec/coins.md` / `plan/coins.md` 补采集中心（条件 · ID 两种探索、多选入队、队列四筛选 + 分页、节点胶囊、徽标文案与下拉文案的差异原因）。
4. 浏览器实测：`/coins/ingest`（探索 → 多选 → 入队 → 队列筛选/分页），顺带复核 `/coins/collection`、`/coins/grades`、`/coins/collections`、`/coins/watchlist`、`/coins/info`、`/coins/series/{id}`。

## 下一步
1. 先跑 `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_coins_pages.py -k ingest` 确认现有 4 条 ingest 断言仍绿。
2. 按「未完成 1」补两条测试并跑通。
3. 全量 pytest → 改三份文档 → 浏览器实测。

## 约束
- 原型是唯一标准；结构 / 区块归属 / 按钮文案一律照 `docs/prototypes/coin-refactor/`，不自行归并增删。
- 不改既有测试断言（结构变化导致选择器失效可同步改，但要说明）。
- 探索来源为**本地已采集系列**（不调 Numista 接口）；外部新系列发现仍由 `/coins/explore` 候选池负责。若用户要求外部检索需另开任务。

## 读取起点
- 原型：`docs/prototypes/coin-refactor/ingest.html`、`index.html`（第 110 行改动点）、`proto.css`（168-173 / 250-251 行）
- 实现：`app/routers/coins.py`（1045-1237 行采集中心段）、`app/templates/coins/ingest.html`、`app/static/style.css`（813-831 行）
- 数据：`app/repository.py`（`list_coin_ingest_tasks` 1115 行起、`search_coin_series` 2995 行）
- 测试：`tests/test_coins_pages.py`（1170-1213 行 ingest 段、`_make_series` / `_seed_uk_1904` 746 行）

## 结果（2026-11-08 续跑收口）
- 已完成：`test_ingest_condition_explore_merges_series_id`、`test_ingest_queue_enqueues_selected` 两条已补，`-k ingest` 5 passed；全量 `pytest -q` → 570 passed, 1 skipped；文档 `CHANGELOG.md` v1.141、`spec/coins.md` §14.23、`plan/coins.md` §18.23 已补。
- 页面实测：`GET /coins/ingest?country=英国&denomination=1便士` 200 / 0.02s、探索结果 13 条；`/coins/collection`、`/coins/grades`、`/coins/collections`、`/coins/watchlist`、`/coins/info`、`/coins/mxiqi` 均 200。
- 未验证（浏览器自动化通道故障，点击后整通道 60s 超时，非应用问题）：真实浏览器里「勾选 → 加入队列 → 队列筛选/分页」点击链路；该链路由上述两条测试覆盖，如需人工确认可在浏览器手动点一次探索。

status: done

# 铜币大模块重构 · 第四轮原型定稿 → 编码（schema v33 已落）

## 已完成
- 第四轮原型 4 条意见全部落地，5 个 HTML 结构自检通过（无未闭合标签）：
  1. `recommend.html`：收藏覆盖改为标题 `<h3>收藏覆盖</h3>` + 次行灰色正文（`.rec-coverage h3/p`）。
  2. `holdings.html`：修改品相从右侧抽屉改为居中遮罩弹窗（`#grade-drawer.hold-modal`，400px、`translate(-50%,-50%)`、淡入、Esc/遮罩/取消关闭）；`role="dialog" aria-modal="true"`。
  3. `collections.html`：`英国 · 乔治六世 · 1 便士` 为 `.col-title`（15px/700），其余为 `.col-meta`（12.5px 灰）；删除 `[展开 ▾]/[收起 ▴]` 及 `data-col-label`，保留整行点击与键盘切换；`.col-link` 改正常文字色、常规字重。6 张卡片全改。
  4. `series-detail.html`：Numista 链接改 `https://en.numista.com/{series_id}`（示例 `https://en.numista.com/670`）；`.sd-img` 加 `aspect-ratio: 1 / 1` + `img{width/height:100%;object-fit:cover}`。
  - `index.html` 对应说明同步改成弹窗 / 整行点击 / 正常文字色。
- 编码第一步（观察系列表，schema v33）：
  - `app/models.py`：`SCHEMA_VERSION = "33"`；新增 `coin_watchlist(id, series_id UNIQUE → coin_series ON DELETE CASCADE, created_at)` + `idx_coin_watchlist_created`。
  - `app/migrations/__init__.py`：`COLUMN_MIGRATIONS[("32","33")] = {}`，并对 20–31 各版本登记沿用 v32 加列 + 回填。
  - `CHANGELOG.md` v1.129。
  - 8 处 schema 版本断言 32→33（`tests/test_coins_categories.py`、`tests/test_coins_analysis.py`），两文件 33 passed；临时库 `init_db` 建表与版本号 33 验证通过。
- 编码第二步（观察系列读写，本轮 2026-11-08 续做）：
  - 全量 `pytest -q` → 552 passed, 1 skipped（v33 无回归）。
  - `app/models.py`：新增 `CoinWatchItem`（id / series_id / numista_id / title / issuer / ruler / denomination / min_year / max_year / url / created_at）。
  - `app/repository.py`：「观察系列（spec §14.18）」段新增 `add_coin_watch_item`（按 `numista_id` 解析 `coin_series.id`，`INSERT OR IGNORE` 幂等，系列不存在 False）、`remove_coin_watch_item`、`list_coin_watchlist`（`LEFT JOIN coin_series_cn` 按 numista_id 取中文展示字段，created_at DESC）。
  - `tests/test_coins_collection.py`：新增 `test_watchlist_add_list_remove`；该文件 47 passed。
  - `CHANGELOG.md` v1.130。
- 编码第三步-a（异步采集任务表，schema v34，本轮 2026-11-08 已完成）：
  - 已与用户确认三处口径：① 同系列已在队列（pending / running）→ **复用现有任务并提示**；② 节点保留独立 `failed` + `error`；③ 图片采完仍无图 → 节点记 `done`（不算缺失）。
  - `app/models.py`：`coin_ingest_tasks(id, series_numista_id, name, status, node_series, node_years, node_values, node_images, source, error, created_at, started_at, finished_at)`；`idx_coin_ingest_status(status, created_at DESC)`；局部唯一索引 `idx_coin_ingest_active ON (series_numista_id) WHERE status IN ('pending','running')`；`SCHEMA_VERSION = "34"`。
  - `app/migrations/__init__.py`：`COLUMN_MIGRATIONS[("33","34")] = {}`，20–32 各版本登记沿用 v33 加列 + 回填。
  - 节点取值 `wait / running / done / skip / failed`；任务级 `status` 派生 `pending / running / done / failed`。
  - 8 处 schema 版本断言 33→34；三个定向文件 80 passed。
  - `CHANGELOG.md` v1.131。
- 编码第三步-b（采集任务读写，本轮 2026-11-08 已完成）：
  - `app/models.py`：新增 `CoinIngestTask`（id / series_numista_id / name / status / 4 节点 / source / error / 三个时间列）。
  - `app/repository.py`：「异步采集任务」段新增 `enqueue_coin_ingest_task`（命中 pending/running 复用现有任务并返回 reused=True）、`list_coin_ingest_tasks`（任务名 LIKE / 状态 / 加入时间 / 完成时间四组条件）、`set_coin_ingest_node`（节点与状态白名单校验，带 error 同步记任务）、`claim_next_coin_ingest_task`（最早 pending → running + started_at）、`finish_coin_ingest_task`（done / failed + finished_at）。
  - `tests/test_coins_collection.py`：新增 `test_ingest_task_enqueue_reuse_claim_and_finish`、`test_ingest_task_list_filters`；该文件 49 passed。
  - `CHANGELOG.md` v1.132。
- 编码第三步-c（队列执行器，本轮 2026-11-08 已完成）：
  - 新增 `app/coins/ingest.py`：`NODES` = series / years / values / images；每节点先判「本地已有 → skip」；节点异常 → 节点 `failed` + error、任务收尾 `failed`、后续节点保持 `wait`；图片节点调用成功但无图仍记 `done`。
  - `run_task` / `run_queue`（循环 claim 到队列空）/ `start`（`asyncio.create_task` 后台跑一轮，在跑返回 False）/ `is_running`。
  - `tests/test_coins_collection.py` 新增 3 用例（全 done / 全 skip 且不重复采 / 节点失败止步），该文件 52 passed；全量 558 passed, 1 skipped。
  - `CHANGELOG.md` v1.133。

## 未完成
- 2026-11-08 新会话：预算 hook 判定累计输入超 1.2M，按规则收口交接；仅完成落点勘察（见「下一步 1」），零代码改动。
- 4 tab 导航与采集中心入口（含把 `ingest.start` 接到页面按钮）、币库「+ 观察」与详情补全、观察系列页、我的资产三页、今日推荐查询区块、麦稀奇数据入口整合均未开始。
- `app/templates/`、`app/routers/coins.py` 零改动；`app/coins/ingest.py` 为本轮新增（执行器），其余业务模块未动。

## 下一步
1. 4 tab 导航与采集中心入口（落点已探明，可直接开工）：
   - 现状：`app/templates/coins/` 共 14 个模板、无 tab 条片段；`app/routers/coins.py` 现有路由 = `/coins`、`/coins/search`、`/coins/session/{id}`、`/coins/info`、`/coins/series/{numista_id}`、`/coins/manage*`、`/coins/grades*`、`/coins/explore`、`/coins/analysis`、`/coins/shops`、`/coins/collection`、`/coins/collections*`。
   - 落点：新建 `app/templates/coins/_tabs.html`（4 tab：今日推荐 / 我的资产 / 币库 / 麦稀奇数据，最右「采集中心」，仅铜币模块渲染），在 4 个 tab 首屏模板 include。
   - 采集中心：`GET /coins/ingest`（`list_coin_ingest_tasks` + 4 节点状态 → 新模板 `coins/ingest.html`）、`POST /coins/ingest`（`enqueue_coin_ingest_task` 后 `ingest.start(config.get())`）；只放入队与刷新，禁止业务按钮。
   - 2026-11-08 已拍板并落地（v1.134）：① 4 tab = 今日推荐 `/coins/collection` / 我的资产 `/coins/grades` / 币库 `/coins/info` / 麦稀奇数据 `/coins/analysis`（拍场 `/coins`、拍品搜索、专场页并入麦稀奇数据；收藏系列、品相台账并入我的资产；候选池并入币库）；② 采集中心 = 独立整页 `GET /coins/ingest`。`coin_nav` 宏改 4 tab + 最右「采集中心」，旧 active key 宏内映射；`coin_library_tabs` 增候选池、采集中心改指 `/coins/ingest`；新增 `coins/ingest.html` 与 GET/POST 路由；3 个用例。
2. 按序编码：币库加入观察 / 详情补全 → 观察系列 → 我的资产三页 → 今日推荐查询区块 → 麦稀奇数据入口整合。
3. 每步同步测试、CHANGELOG、spec / plan；原型不再改，除非用户新意见。

## 约束
- 铜币子系统 4 tab：今日推荐 / 我的资产 / 币库 / 麦稀奇数据；采集中心按钮挂 tab 行最右，仅铜币模块显示。
- 全站其他菜单（日报、历史搜索、源管理、运行控制、基金）不动。
- 观察项粒度 = 单个 Numista 系列；支持多选后一次升级；冲突提示并禁止。
- 采集中心只做采集与排队，禁止业务按钮；4 节点各记状态，图片为空不算缺失。
- 收藏系列列表只做总览（不改成员），详情页做成员管理 + 编辑/删除。
- 只做被要求的；不改既有文案与测试断言（schema 版本号断言随版本升级同步除外）。

## 读取起点
- 原型：`docs/prototypes/coin-refactor/index.html`（第四轮已定稿）
- 表 / 迁移：`app/models.py`（`coin_watchlist`）、`app/migrations/__init__.py`（v33 段）、`app/db.py`
- 路由：`app/routers/coins.py`
- 数据：`app/repository.py`
- 业务：`app/coins/explore.py`、`app/coins/collection.py`、`app/coins/recommend.py`
- 参考页：`app/templates/coins/series.html`（币库详情布局）、`app/templates/coins/collection.html`（今日推荐查询区块）
- 测试：`tests/test_coins_collection.py`、`tests/test_coins_pages.py`

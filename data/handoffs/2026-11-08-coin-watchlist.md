status: done

# 铜币重构 · 交接项已清零（四页已实测通过）

## 2026-11-08 第 4 段：文档补齐 + 麦稀奇数据入口页（v1.139 / v1.140）
- `spec/coins.md` §14.21（收藏系列列表 / 详情 / 收藏覆盖）、§14.22（麦稀奇入口页）；`plan/coins.md` §18.21 / §18.22 已补。
- `CHANGELOG.md` v1.139（纯文档）、v1.140（入口页）。
- 新增 `GET /coins/mxiqi` + `app/templates/coins/mxiqi.html`：四张入口卡 `​/coins`、`/coins/search`、`/coins/analysis`、`/coins/shops`；`coin_nav` 的「麦稀奇数据」改指该页，active 映射补 `mxiqi` / `data`；`style.css` 增 `.coin-entry-grid` / `.coin-entry`。
- `tests/test_coins_pages.py` 新增 `test_mxiqi_page_links_existing_pages`；该文件 97 passed。
- 2026-11-08 第 5 段：浏览器实测 PASS —— `/coins/grades`（961 组合行 + 统计副标题 + 新增组合面板可展开）、`/coins/collections`（75 张收藏系列卡片）、`/coins/collections/1`（系列概况 已有 12 / 缺 16 / 升级 2 + 成员管理 3）、`/coins/mxiqi`（四张入口卡）；四页无 500 / Traceback。
- 注意：旧 uvicorn 进程仍是 v1.139 之前的代码（schema 32、无 `/coins/mxiqi`），需重启服务才生效。

## 2026-11-08 第 3 段：collections 改卡片 + collection-detail 已落地
- `app/routers/coins.py`：抽 `_collection_overview()`（列表 / 详情共用）；`collections_page` 改用它；新增 `GET /coins/collections/{collection_id}`（`collection_detail_page`，不存在返回 404）；成员加入 / 移出回跳改到 `/coins/collections/{id}#member-panel`。
- `app/templates/coins/collections.html`：改 `article.coin-collection-card` + `.col-row`（摘要 + 「进入管理 →」）+ `.col-body`（ID / 进度 / 年份胶囊 / 成员胶囊，默认 `hidden`，行内 JS 切换）；管理区（表格 / 加成员 / 编辑 / 删除 / 保存品相）全部移出。
- `app/templates/coins/collection_detail.html`（新）：系列概况（进度 / 年份 / 保存品相）+ 成员管理（筛选 / 加入 / 移出，纪念币多两列）+ 编辑收藏系列 + 删除。
- `app/static/style.css`：新增 `.col-row` / `.col-summary` / `.col-meta` / `.col-link` / `.col-body` / `.coin-collection-id` / `.coin-panel*`。
- `tests/test_coins_pages.py`：新增 `test_collections_page_shows_rows_and_detail_link`、`test_collection_detail_page_lists_members_and_404s`；**该文件 96 passed**。
- 顺手：删除已无引用的 `_attach_collection_names`。
- 未做：`spec/coins.md` §14.2x、`plan/coins.md` §18.2x 未补；浏览器实测未跑；`CHANGELOG.md` 已有 v1.137。


## 已完成
- 2026-11-08 续做（schema 已到 v34，全量基线 563 passed, 1 skipped）：
  - `app/routers/coins.py`：新增 `POST /coins/watchlist/add`（`numista_id` + `next`，回跳经 `_return_target` 限 `/coins/`）；`info_page` 传 `watched_ids` / `next_url`；`series_page` 传 `is_watched`。
  - `app/templates/coins/info.html`：卡片操作区只留「加入观察 / 已观察（disabled）」，删除「补全 / 详情」按钮（整卡仍是到详情的链接）。
  - `app/templates/coins/series.html`：改 `coin-panel` + `.coin-series-info`（「系列信息」+ 右侧观察按钮）+ `.sd-series-grid`（图片左、`coin-facts-3col` 右，18 格）；补齐数据三按钮移入右栏；Numista 链接统一 `https://en.numista.com/{series_id}`。
  - `app/static/style.css`：新增 `.coin-series-info` / `.sd-series-grid` / `.sd-imgs` / `.coin-facts-3col`，900px 以下单列。
  - `tests/test_coins_pages.py`：新增 `test_info_page_watch_button_adds_and_is_idempotent`、`test_series_page_watch_button`；该文件 94 passed。
  - `CHANGELOG.md` v1.135；`spec/coins.md` §14.20。
- 观察系列页（v1.136，本会话已完成）：
  - `app/routers/coins.py`：`GET /coins/watchlist`（五条件查询，按观察时间倒序）、`POST /coins/watchlist/remove`。
  - `app/repository.py`：`search_coin_series` 加 `numista_ids` 过滤（空列表直接返回空）。
  - `app/templates/coins/watchlist.html`（新）：单列卡片 + 正反图 + 放弃观察（表单 POST）。
  - `app/templates/coins/_macros.html`：`coin_library_tabs` 加「观察系列」。
  - `tests/test_coins_pages.py`：新增 `test_watchlist_page_lists_filters_and_removes`；该文件 95 passed。
  - plan §18.20 已落盘（含第 6 条观察系列页）；`spec/coins.md` §14.20 已补页面与验收；`CHANGELOG.md` v1.136。
  - 本轮不做：全选 + 「升级选中」（升级到收藏口径未定，等确认）。

## 未完成
- 我的资产三页（holdings / collections / collection-detail 原型）、今日推荐查询区块、麦稀奇数据入口整合。
- 观察系列页的「全选 + 升级选中」：升级到收藏的口径未定，等确认（当前只有单个「放弃观察」）。
- 候选池系列详情不显示观察按钮（已按此实现）；观察系列页入口已进 `coin_library_tabs`（系列库 / 候选池 / 观察系列 / 采集中心）。

## 2026-11-08 第 2 段：holdings 已落地
- 用户确认：可改既有断言 / 子 tab 改名「个人收藏清单」/ 删「所属收藏系列」列。
- `app/templates/coins/_grades_results.html`：去整页保存表单；品相只读 `coin-grade-value`；操作列 `⋯` 菜单（修改品相 / 删除二次确认）。
- `app/templates/coins/grades.html`：标题「个人收藏清单」+ 统计副标题；新增组合收进 `hold-add-panel`（默认 `hidden`，`#toggle-add` 展开）；新增遮罩弹窗 `#grade-drawer`（单条 POST `/coins/grades/save`）。
- `app/routers/coins.py`：`page_title` 改名；`_grades_context` 不再调 `_attach_collection_names`（**函数已无引用，待删**）。
- `app/templates/coins/_macros.html`：`coin_collection_tabs` 文案改名。
- `app/static/style.css`：新增 `.hold-add-panel` / `.hold-form-grid` / `.coin-more*` / `.hold-scrim` / `.hold-modal*`。
- `tests/test_coins_pages.py`：改 `test_grades_page_lists_combos_with_dropdown`、`test_grades_page_filters_by_grade`、`test_grades_save_upserts_and_clears`、`test_grades_add_fragment_returns_results_only`；删 `test_grades_page_derives_collection_series`。**该文件 94 passed**。
- 未做：CHANGELOG / spec §14.2x / plan §18.2x 未写；浏览器实测未跑。

## 未完成
1. collections（收藏系列）：`col-row` / `col-body` + 「进入管理 →」。
2. collection-detail：新增 `GET /coins/collections/{collection_id}` + 模板 + 概况统计。
3. 补文档；删 `_attach_collection_names`。

## 下一步
1. 我的资产三页：holdings / collections / collection-detail（按原型；原型里观察系列挂在「我的资产」子导航，落地时已改放币库子导航，后续页面沿用币库一侧）。
2. 今日推荐查询区块。
3. 麦稀奇数据入口整合。

## 约束
- 4 tab：今日推荐 `/coins/collection` / 我的资产 `/coins/grades` / 币库 `/coins/info` / 麦稀奇数据 `/coins/analysis`；采集中心最右。
- 观察粒度 = 单个 Numista 系列；已观察置灰；候选池不显示观察按钮。
- 不改既有文案与测试断言（schema 版本断言除外）；只做被要求的。

## 读取起点
- 原交接：`data/handoffs/2026-11-08-coin-refactor-prototypes.md`
- 原型：`docs/prototypes/coin-refactor/watchlist.html`
- 路由：`app/routers/coins.py`（`add_watch_item` / `info_page` / `series_page`）
- 数据：`app/repository.py`（`add_coin_watch_item` / `remove_coin_watch_item` / `list_coin_watchlist`）
- 模板：`app/templates/coins/info.html`、`series.html`、`_macros.html`
- 测试：`tests/test_coins_pages.py`（两个观察用例）

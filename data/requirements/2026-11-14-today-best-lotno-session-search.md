# 需求卡：今日拍场好品计数 + 推荐卡 lot_no + 专场好品区 + 专场全量搜索

日期：2026-11-14　｜　类型：P（页面）　｜　涉及 4 条需求，建议拆 2 张卡执行（1/2 一张，3/4 一张）

## 1. 验收句

1. 今日拍场 rail 某场有精准 2 件、好品 1 件、相似 3 件时，该场 meta 显示「精准 2 · 好品 1 · 相似 3」，圆圈数字为 6。
2. 某场仅有好品命中（hit_count=0、similar_count=0、best_count>0）时，该场出现在「今日拍场」列表且圆圈数字等于 best_count，不出现 0 件条目。
3. 今日推荐页精准卡、相似卡、好品卡中，某拍品 lot_no=「Lot 123」时，拍品名称行渲染为「Lot 123 拍品名」（lot_no 为空时不留多余空格）。
4. 拍场详情页精准推荐 section 之后紧跟「好品推荐」section，计数等于该专场内命中的好品拍品数；该专场无好品时显示空态文案且不报错。
5. 拍场详情页在搜索框输入只在第 2 页出现的拍品编号或标题片段时，搜索结果出现该拍品（当前在第 1 页亦可命中）。
6. 收起/隐藏推荐模块（hide_rec=1）时，新增的好品推荐 section 与其他推荐区一同隐藏。

## 2. 待确认

1. 好品计数的去重口径：同一 lot 同时命中精准与好品时怎么算？
   - A（推荐）按 lot_id 取并集，圆圈 = 三类去重后的拍品数；summary 行同步加「好品 N 项 · 命中 M 件」。
   - B 类目各自累加，允许重复计数；summary 不动。
2. 专场好品区卡片形态：
   - A（推荐）复用 `lot_card`，仅加「好品推荐」标签与评级分数/铜色一行，避免新样式。
   - B 新写专用卡片宏，版式贴近今日推荐页 `render_best_card`。
3. 全专场搜索的实现路线：
   - A（推荐）路由传轻量 `search_index`（id/href/lot_no/title/shop），JS 渲染紧凑结果行，不克隆卡片、不加载图片。
   - B 服务端 `?q=` 过滤后直出结果页（需 repository 支持关键字查询，未确认）。
   - C 模板内 `<template>` 放全量卡片池（实现最省，但 HTML 随专场件数线性膨胀）。

## 3. 改动链

| 文件 | 函数 / 模板 / 测试 |
|---|---|
| app/routers/coins.py | collection_page（L2059）：best_items（L2106）按 session_id 聚合 best_count（L2217-2298 的 today_sessions）；rail_sessions 过滤条件加 best_count |
| app/templates/coins/collection.html | rail meta（L227）、圆圈（L231）、summary（L216-219，视待确认 1）；拍品名 L49、L73、L84 |
| app/models.py | CoinTodayLot（L1292）、CoinBestLot（L1308）各加 `lot_no: str \| None = None` |
| app/repository.py | `_list_coin_lots_between`（L1439）、`list_best_grade_lots`（L1644）SQL 补 `l.lot_no` |
| app/coins/best.py（路径待确认） | `load_best_lots(cfg)` 复用；session_page 按 session_id 过滤 |
| app/routers/coins.py | session_page（L284）：新增 best_lots / search_index 上下文 |
| app/templates/coins/session.html | 精准区（L120-127）后插入好品 section；JS recSections（L173）加入新 id；搜索集合与结果渲染（L184-205） |
| tests/test_coins_pages.py | 新增：rail 好品计数与仅好品场次、三卡 lot_no、专场好品区与空态、跨分页搜索；现有 `test_session_page_has_lot_search_and_hide_rec_toggle`（L537-549）不改断言 |

## 4. 取舍

1. 好品计数：在 `today_sessions` 上就地加字段（推荐，零额外查询）vs 重新跑一次聚合 SQL（多一次全表扫描，无收益）。
2. lot_no：模型加字段 + SQL 补列（推荐，三处共用）vs 模板层联查（会引入 N+1）。
3. 搜索：JSON 索引前端渲染（推荐，无翻页依赖、无图片请求、体积约 100B/件）vs 服务端 q（少前端代码但交互从即时过滤变整页刷新）vs 全量卡片池（实现最省、体积风险最高）。
4. 好品区数据：直接过滤 `load_best_lots` 的当日结果（推荐，与今日推荐口径一致）vs 新写「按专场查好品」SQL（口径可能漂移）。

## 5. 明确不做

- 不改任何既有文案（如「今日拍场」「其他拍品」标题）与既有测试断言。
- 不改好品阈值 `BEST_GRADE_THRESHOLD`、11070 类目范围、`COIN_OTHER_LOT_PAGE_SIZE`。
- 不改精准/相似推荐算法与排序、去重规则之外的匹配逻辑。
- 不做与需求无关的样式调整、重命名、重构，不新增图片懒加载组件。
- 不动其他页面（`/coins`、`/coins/search`、采集中心等）。
- 不为专场搜索新增缓存、索引表或迁移脚本。

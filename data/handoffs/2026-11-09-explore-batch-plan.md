# 2026-11-09 采集中心「探索条件 · 批量方式(页面)」实现方案

status: superseded

> 已被 [2026-11-09-explore-batch-verify.md](2026-11-09-explore-batch-verify.md) 取代。方案已落地，DDL / 端点 / 字段以 `app/models.py`、`app/coins/explore_page.py`、`app/routers/coins.py` 为准。

## 1. 文件清单与改动点

| 文件 | 位置 | 动作 |
|---|---|---|
| `app/models.py` | `SCHEMA_VERSION`（L7）、`coin_explore_candidates` 之后（L447-464） | 版本 34→35；新增 `coin_page_explore_items` DDL + 索引 |
| `app/migrations/__init__.py` | 文件末尾登记表 | 登记 (34,35) 纯新增表升级（与既有 `FORWARD_ADDITIVE_UPGRADES` 同格式，具体常量名以文件末尾为准） |
| `app/repository.py` | `upsert_coin_explore_candidate` 之后（L1224-1289） | 新增 `upsert_coin_page_explore_item` / `list_coin_page_explore_items(batch_id)` |
| `app/coins/numista_web.py` | `CatalogueItem`（L119）、`parse_catalogue_index`（L255-306）、`fetch_catalogue_page`（L400-433） | 扩字段 ruler/denomination；年份只取首个括号前；`fetch_catalogue_page` 加可选 `url` 参数 |
| `app/coins/explore_page.py`（新建） | — | 选项常量、URL 拼装、单飞后台任务、进度状态、>50 判定、入库编排 |
| `app/coins/translate.py` | `GLOSSARY`（L26 起） | 只补页面新增英文词条（材质变体、`Coins` 子类），不新建第二份词表 |
| `app/routers/coins.py` | import 区（L20-40）、`ingest_page`（L1155-1255） | 注入选项/进度；新增 2 个页面端点 |
| `app/templates/coins/ingest.html` | ID 方式区块后（L47）与探索结果标题（L51-52） | 新区块、链接展示、按钮状态、轮询脚本 |
| `tests/test_coins_numista_web.py` | L127-170 附近 | 解析器 / URL 用例 |
| `tests/test_coins_explore_page.py`（新建） | — | 服务层入库、跳过、>50 用例 |
| `tests/test_coins_pages.py` | L1207-1310 附近 | 页面区块 / 端点用例 |

## 2. 数据表设计（建议新表）

`coin_page_explore_items`（不复用 `coin_explore_candidates`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| batch_id | TEXT NOT NULL | 一次「获取列表」一个批次号（时间戳），用于只展示本批 |
| numista_id | INTEGER NOT NULL UNIQUE | series id，主键语义；已存在跳过 |
| title / title_cn | TEXT | 名称（英 / 中） |
| issuer / issuer_cn | TEXT | 国家（原始多级串 / 中文末级） |
| ruler / ruler_cn | TEXT | 执政者 |
| denomination / denomination_cn | TEXT | 面值 |
| year_text | TEXT | 原始年份文本 |
| min_year / max_year | INTEGER | 只取首个括号前的区间 |
| composition / composition_cn | TEXT | 材质 |
| size_mm | REAL | 尺寸 |
| source_url | TEXT | 本题链接 |
| created_at / updated_at | TEXT NOT NULL | |

索引：`idx_coin_page_explore_batch(batch_id)`；唯一约束 `numista_id`。

> 落地偏差：`SCHEMA_VERSION` 实际到 **36**（不是 35）；另加成员表 `coin_page_explore_batch_items`，同批重跑按批次 ID 集合过滤，行上 `batch_id` 不更新。

理由：`coin_explore_candidates` 是候选池的业务表（status/seed_*/payload JSON/采纳流转，被 `/coins/explore` 消费）；本需求是「一次性列表展示」，字段是拆开的结构化列、无采纳状态，复用会把页面结果混进候选池并需伪造 payload/verified_at。

## 3. 后端端点

| 方法 | 路径 | 入参 | 返回 / 错误 |
|---|---|---|---|
| POST | `/coins/ingest/page-explore` | form: `issuer`(code)、`st`、`year`、`material`、`size` | 200 JSON `{job_id, url}`；400 条件非法；409 已有任务在跑 |
| GET | `/coins/ingest/page-explore/status` | `job_id` | JSON `{job_id,status,total,parsed,saved,skipped,percent,error_code,error_message,items}`，`status`=running/done/failed |
| GET | `/coins/ingest`（既有） | 既有参数 | 模板上下文新增 `page_explore_options`、`page_explore_job` |

选项来源（静态即可）：国家 = `app/coins/numista.py` 的 `ISSUER_CODES`（中文名→code，L65-126）；货币类型 = `st` 常量（1/2/3/47/154/5/6）；材质 = `m` 常量（3 铜、5 青铜、4 黄铜、2 白铜、1 银、6 金、9 钢、45 铝、10 铝青铜、bi 双金属）。三者都是闭集，放 `explore_page.py` 常量并注入模板，不新增接口。URL 由后端按入参重算，不信任前端传链接。

## 4. 后台任务与进度

- 不复用 `app/coins/ingest.py`：那是「系列 4 节点」队列，进度单位是任务行，与本需求的一次性页面抓取语义不同。
- 新模块 `app/coins/explore_page.py`，沿用 `ingest.py` 的单飞模式：模块级 `_running` + `_state`（job_id / status / total / parsed / saved / skipped / error_code），`asyncio.create_task` 启动；同一时刻只允许一个任务。
- 进度：拿到首页 HTML 后先取 `results found` 总数；`percent = parsed/total*100`，入库完成置 100；失败保留已入库行。
- 轮询：前端 `setInterval` 约 800ms 调 status，`done`/`failed` 即停；`items` 只在结束时返回（≤50 行），页面就地替换结果表。

## 5. 解析器与 >50 风控

- `CatalogueItem` 增加 `ruler` / `denomination`（默认空）；`parse_catalogue_index` 里 `title.partition(" - ")`：有分隔符 → 面值 / 执政者；无分隔符 → 两者留空，不硬拆。
- 年份：先截断到第一个 `(` 之前，再取 `\d{3,4}` 首尾；`1940-1950 (1883-1893)` → 1940/1950。
- 国家：原文入库；中文取 `›` 最后一级后走 `translate.to_zh`，词表未命中保留英文。
- URL：直接构造 `ExploreCatalogueConfig(issuer, collectible_type, year, composition_id, diameter_mm)` 复用 `catalogue_url`（L217-252）。
- `fetch_catalogue_page` 增加 `url: str | None`，传入即跳过配置拼装；`_wait_for_catalogue` 仍按 `results found` 判定。
- >50 判定在服务层、写库前：首页 HTML → `_CAT_TOTAL` 总数 > 50 → 立即 `failed`，`error_code=over_limit`，文案「当前探索结果超过 50 条，请重新筛选条件」，不写任何行。翻页上限同时按 50 封顶。

## 6. 前端

- 位置：`ingest.html` 的「探索条件 · ID 方式」`</section>` 之后、探索结果之前。
- 表单：国家（select，中文显示、值 code）、货币类型（select）、年份（text）、材质（select）、尺寸（text）+【探索】。
- 第二行：只读链接（input/link）+【获取列表】（探索前 disabled；任务中 disabled 显示「采集中」）。
- 探索结果标题右侧（L51-52）：`正在采集：{{percent}}%`，完成后恢复「多选后一次加入队列」；失败显示错误文案。
- 不需要新增样式类名时复用 `coin-panel` / `coin-form-grid` / `search-input` / `btn`。

## 7. 测试清单

- `tests/test_coins_numista_web.py`：`test_parse_catalogue_index_splits_denomination_and_ruler`、`test_parse_catalogue_index_year_ignores_secondary_range`、`test_fetch_catalogue_page_uses_explicit_url`。
- `tests/test_coins_explore_page.py`：`test_page_url_maps_chinese_filters`、`test_run_page_explore_over_50_stops_before_write`、`test_run_page_explore_upserts_and_skips_existing`、`test_run_page_explore_single_flight`。
- `tests/test_coins_pages.py`：`test_ingest_page_renders_page_explore_section`、`test_page_explore_start_conflict_when_running`、`test_page_explore_status_reports_progress`。

## 8. 需要拍板

1. **表复用**：推荐新表 `coin_page_explore_items`；备选复用 `coin_explore_candidates`（加 `source` 列、payload 塞页面字段）——会污染候选池。（已拍板：采用新表）
2. **国家数据源**：推荐静态 `ISSUER_CODES`（约 60 国，中文→code，零网络）；备选 `search_issuers.php?q=&p=` 常用子集（覆盖广，需接口 + 缓存）；备选 `/v3/issuers` 全量（12,028 条，需分页搜索）。（已拍板：静态 `ISSUER_CODES`）
3. **进度存储**：推荐内存单飞（与 `ingest.start` 一致，重启丢失可接受）；备选新增 job 表（可重启续跑、多进程安全，成本多一张表 + 清理）。（已拍板：内存单飞）

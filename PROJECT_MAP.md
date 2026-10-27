# 项目地图（代码导航）

> 目的：定位改动点时直接跳到文件与函数，不靠宽关键词反复搜。
> 只记「现在是什么样」；变更原因见 [CHANGELOG.md](./CHANGELOG.md)。
> 读取时优先 `Grep -n -C N` 取片段，不整份读大文件（[RULES.md](./RULES.md) §四）。
>
> **用法**：新需求 → 先按 [task-routing.md](./docs/task-routing.md) 判类型（Q / D / S / P / L）→ 第 0 节定位 →
> S / P / L 写四行任务卡（类型 / 需求原句 / 改动链 / 轮数上限）：S 走下方五轮模板，P 先出原型，L 先让 `planner` 出需求卡；Q / D 只读、不写卡。

## 速查：S · 日常小改（≤10 轮）

> 五轮模板与下方六条的**唯一源**；[RULES.md](./RULES.md) §二 / §七 只留判据与预算，不再重复本表。
> 适用：需求一句话能复述完、只改一条链路。

| 轮次 | 动作 | 硬约束 |
|---|---|---|
| 1. 定位 | 按改动链做**精确符号**搜索，3~5 个同轮并行 | 搜索 ≤6，超 6 次立即停、回「0. 30 秒定位」按改动链跳文件；不用 `year\|issue` 宽词；同文件换词 ≤2，第 3 次前先 `Grep '^(class|def) ' -n` 出符号表；0 命中算事故 |
| 2. 收集 | 并行读命中片段：`Read` 带 `offset/limit`（单段 ≤120 行）或 `Grep -C 6` | 文件 ≤8；不整份读模板 / 测试；同文件 ≤1 次调用、累计 ≤150 行，第二次读须在回执写明新增区间 |
| 3. 编辑 | 同轮批量改；同文件多处改动合并 | 只改需求点名的地方（见下） |
| 4. 验证 | 定向跑改动文件，最后全量 `pytest -q` **最多 1 次**，只留末尾汇总 | 失败只重跑失败文件或 `-k`；同会话第 2 次全量必须加 `# rerun` 并写明上次失败点；命令输出先 `tail -n 30` 或只取 count 汇总 |
| 5. 文档 | CHANGELOG 一条 + spec / plan 命中段落原地改 | 不为一次小改翻 4 个文档 |

**每轮收口**（每轮回复前必做，不占轮次预算）：`回执：` + 本轮 token 成本清单（[RULES.md](./RULES.md) §零.3）；
出表用 `.venv/bin/python utils/token_cost/token_cost.py --round`，只读上次出表后新增的请求，中断 / 换会话都不丢账。

**只做被要求的（§七）**

1. 需求里有几个名词 / 动词，就只改与之对应的几处；文案、提示、校验、命名、样式、错误处理一律不做。
2. 不顺手改既有行为（尤其既有文案、既有测试断言）；发现别处可优化 → 只报告、不动手。
3. 不为不可能发生的场景写代码（选不出的下拉值、填不出的组合）。
4. 不做与需求无关的重命名 / 重构。
5. 验证与文档按体量配比：一行筛选条件 = 一条用例 + CHANGELOG 一行。
6. 边界模糊先问，不自己扩。

**提交前自检**：diff 逐条对着需求原句问「需求里哪句话要它？」说不出来 → 撤掉或单独列出来问；
「顺手做了」的改动单独列在回复末尾。

## 0. 30 秒定位

| 需求类型 | 首选文件（按顺序） |
|---|---|
| 页面加字段 / 改展示 | `app/routers/coins.py`（路由与上下文）→ `app/templates/coins/<页>.html` → 数据来源模块 |
| 收藏画像 / 推荐 | `app/coins/collection.py` → `app/coins/recommend.py` → `app/models.py`（`CoinYearSpec` 等） |
| 拍场采集 / 解析 | `app/coins/gapi.py` → `app/coins/pipeline.py` → `app/repository.py` |
| Numista 系列 / 年份 / 估值 | `app/coins/numista.py`（接口）/ `app/coins/numista_web.py`（类型页） |
| 品相 / 年份口径统一 | `app/coins/grades.py` |
| 入库 / 查询 SQL | `app/repository.py`（唯一允许写 SQL 的地方） |
| 建表 / 迁移 | `app/models.py`（SCHEMA）→ `app/migrations/v*.py` → `app/db.py` |
| 样式 | `app/static/style.css`（先读 `:root` 变量块 + 对应组件段） |
| 页面冒烟测试 | `tests/test_coins_collection.py`（画像）/ `tests/test_coins_pages.py`（其余页面） |

## 1. 入口与装配

| 路径 | 职责 |
|---|---|
| `app/main.py` | FastAPI 装配、lifespan、路由注册 |
| `app/config.py` | 配置加载 / 校验（`config/config.yaml`） |
| `app/db.py` | 建库、schema_version、迁移调度 |
| `app/scheduler.py` | 每日任务 |
| `app/switches.py` | 功能开关 |
| `app/tasks.py` | 后台任务编排 |
| `app/cli.py` | 命令行入口 |

## 2. 路由 → 模板

| 路由 | 路由文件 | 模板 |
|---|---|---|
| `/`、`/m/{module}` | `app/routers/digest.py` | `digest.html`、`module.html` |
| `/search` | `app/routers/search.py` | `search.html` |
| `/sources` | `app/routers/sources.py` | `sources.html` |
| `/control` | `app/routers/control.py` | `control.html` |
| `/funds` | `app/routers/placeholders.py` | `placeholder.html` |
| `/coins`、`/coins/session/{id}` | `app/routers/coins.py` | `coins/sessions.html`、`coins/session.html`（拍场） |
| `/coins/search` | 同上 | `coins/search.html`（全局拍品搜索） |
| `/coins/manage`、`/coins/info`、`/coins/series/{id}` | 同上 | `coins/manage.html`（采集中心）、`coins/info.html`（币库）、`coins/series.html`（系列详情） |
| `/coins/explore` | 同上 | `coins/explore.html`（候选池） |
| `/coins/analysis` | 同上 | `coins/analysis.html`（分类分析：默认硬币 + 裸币） |
| `/coins/grades` | 同上 | `coins/grades.html`（品相台账） |
| `/coins/collection` | 同上 | `coins/collection.html`（今日推荐：精准匹配 + 相似推荐 + 拍场角标） |
| `/coins/collections` | 同上 | `coins/collections.html`（收藏系列） |

> 旧版标签画像页面 `/coins/profile` 已移除；`coin_profile_tags` 数据与“未配置收藏系列时回退标签画像”的后端逻辑保留。

## 3. 铜币数据链路

```
mxiqi gapi（拍场 / 拍品）── 商品信息 ──┐
Numista API（系列 / 年份）─────────────┼→ app/repository.py（SQLite）→ collection / recommend → 页面
Numista 类型页（估值 / 稀有度）────────┘            ↑
历史订单 xlsx / 人工品相 ──────────────────────────┘
```

| 模块 | 关键函数 |
|---|---|
| `app/coins/collection.py` | `build_year_specs`、`build_profile(s)`、`load_profiles`、`load_explore_profiles` |
| `app/coins/explore.py` | `build_seeds`、`match_seed`、`scan_candidates`、`load_candidate_profiles`、`adopt_candidate` |
| `app/coins/recommend.py` | `build_recommendations`、`year_labels`、`match_lots`、`load_recommendations` |
| `app/coins/search.py` | `search_lots`（全局拍品搜索：FTS 候选 + 推荐标签 + 排序） |
| `app/coins/matcher.py` | `match_lot`、`rematch_all` |
| `app/coins/coinmap.py` | `match_lot`、`remap_all`、`load_catalogue`（拍品→Numista 币种映射） |
| `app/coins/profile.py` | `read_orders`、`parse_extraction`、`rebuild_profile` |
| `app/coins/grades.py` | `normalize_country/year/denomination`、`build_combos` |
| `app/coins/gapi.py` | `MxiqiGapi`、`card_from_auction`、`detail_from_payload` |
| `app/coins/numista.py` | `NumistaClient`、`series_from_type`、`issue_from_issue` |
| `app/coins/numista_web.py` | `parse_type_page`、`parse_catalogue_index`、`load_catalogue_index` |
| `app/coins/pipeline.py` | 每日采集编排 |
| `app/coins/pricing.py` | `summarize` |

## 4. 数据表（`app/models.py`）

| 分组 | 表 |
|---|---|
| 知识聚合 | `sources`、`items`、`item_scores`、`fetch_runs` |
| 铜币拍卖 | `coin_shops`、`coin_sessions`、`coin_lots`、`coin_lot_fts`（FTS5 搜索索引）、`coin_price_refs`、`coin_fetch_runs`、`coin_api_usage` |
| Numista | `coin_series(_cn)`、`coin_issues(_cn)`、`coin_issue_values` |
| 收藏 | `coin_orders`、`coin_purchase_grades`、`coin_profile_tags`、`coin_collection_series(_members)`、`coin_explore_candidates` |
| 系统 | `schema_meta`、`feature_switches` |

## 5. 测试地图

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_coins_collection.py` | 画像纯函数、三层推荐、映射 CRUD、`/coins/collection` 与 `/coins/collections` 页面 smoke |
| `tests/test_coins_pages.py` | 其余铜币页面（偏好标签、拍场、管理、品相…） |
| `tests/test_coins_fetch.py`、`test_coins_numista.py`、`test_coins_numista_web.py` | 采集与解析 |
| `tests/test_coins_match.py`、`test_coins_price.py`、`test_coins_translate.py`、`test_coins_profile.py` | 匹配、定价、翻译、订单抽取 |
| `tests/test_repository.py` | SQL 层 |
| `tests/conftest.py` | 公共夹具（`no_scheduler` 等） |

## 6. 文档地图

| 文档 | 读哪里 |
|---|---|
| `spec/coins.md` | 铜币需求；收藏画像 = §14.17 |
| `plan/coins.md` | 铜币实现细节（搜关键词定位小节） |
| `spec/knowledge.md`、`plan/knowledge.md` | 知识聚合 |
| `spec/core.md`、`plan/core.md` | 通用能力（配置、调度、日志） |
| `tasks/<模块>.md` | 任务与验证证据；已完成模块默认不读 |
| `RULES.md` | 读取 / 轮次 / 对话 / 输出 / 变更边界（≤100 行，只留判据） |
| `docs/token-cost.md` | 回执成本表的口径、校准、对账 |
| `docs/task-routing.md` | 任务类型判定、五类路径、子代理调用与历史校准 |
| `docs/doc-policy.md` | 文档增补往哪写、编号与分册 |
| `CHANGELOG.md` | 时间线，只看最上方几条 |

## 7. 门禁（hook）

配置在 `.trae/hooks.json`，每次调用写 `data/hook_audit.log`，契约测试见 `tests/test_hooks_contract.py`。
阈值（120 行 / 0.8 / 20 行）只定义在 `.trae/rules/project-rules.md`，hook 消费同一数字，不许各写一套。

| 脚本 | 时机 | 拦什么 | 被拒后 |
|---|---|---|---|
| `guard_read.py` | PreToolUse `Read` | 单段 >120 行、整份读、同区间重读（重复率 >0.8 且新增 <20 行） | 按文案给的 `offset` / `limit` 只取未读片段，不要原样重试 |
| `guard_search.py` | PreToolUse `Grep`/`Glob`/`LS` | 同 pattern 同范围重搜、父目录搜过后缩范围 | 换 pattern，或说明范围为何变了 |
| `guard_pytest.py` | PreToolUse `RunCommand` | 同目标重复验证（管道 / 重定向 / `-q` 不算新命令） | 换测试目标；口径变化要说明理由 |
| `guard_stop.py` | Stop | 回执缺 `N / M` 轮数，或成本清单无数字 | 按文案补齐三段 |

原则：**能从 tool_call 字面算出来的判据才进 hook；需要任务意图的留在 rules**。hook 比 rules 贵（一次 deny = 多一轮完整上下文），所以能放行给 `hint` 就不 deny。

「同文件换到第 3 个关键词前先出符号表」目前是**规则层判据**（`.trae/rules/project-rules.md`「读取与输出」第 1 条），`guard_search.py` 尚未消费；接入时阈值共用同一数字 3，不另写一套。

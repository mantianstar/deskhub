# 项目地图（代码导航）

> 目的：定位改动点时直接跳到文件与函数，不靠宽关键词反复搜。
> 只记「现在是什么样」；变更原因见 [CHANGELOG.md](./CHANGELOG.md)。
> 读取时优先 `Grep -n -C N` 取片段，不整份读大文件（[RULES.md](./RULES.md) §四）。
>
> **用法**：新需求 → 本文件第 0 节定位 → 写三行任务卡（需求原句 / 改动链 / 轮数上限）→
> 按 [RULES.md](./RULES.md) §二「轻量任务模板」执行。

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
| `/coins`、`/coins/session/{id}` | `app/routers/coins.py` | `coins/sessions.html`、`coins/session.html` |
| `/coins/profile` | 同上 | `coins/profile.html`（偏好标签） |
| `/coins/manage`、`/coins/info`、`/coins/series/{id}` | 同上 | `coins/manage.html`、`coins/info.html`、`coins/series.html` |
| `/coins/grades` | 同上 | `coins/grades.html`（品相录入） |
| `/coins/collection` | 同上 | `coins/collection.html`（收藏画像 + 三层推荐） |
| `/coins/collections` | 同上 | `coins/collections.html`（收藏系列映射） |

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
| `app/coins/recommend.py` | `build_recommendations`、`year_labels`、`match_lots`、`load_recommendations` |
| `app/coins/matcher.py` | `match_lot`、`rematch_all` |
| `app/coins/profile.py` | `read_orders`、`parse_extraction`、`rebuild_profile` |
| `app/coins/grades.py` | `normalize_country/year/denomination`、`build_combos` |
| `app/coins/gapi.py` | `MxiqiGapi`、`card_from_auction`、`detail_from_payload` |
| `app/coins/numista.py` | `NumistaClient`、`series_from_type`、`issue_from_issue` |
| `app/coins/numista_web.py` | `parse_type_page` |
| `app/coins/pipeline.py` | 每日采集编排 |
| `app/coins/pricing.py` | `summarize` |

## 4. 数据表（`app/models.py`）

| 分组 | 表 |
|---|---|
| 知识聚合 | `sources`、`items`、`item_scores`、`fetch_runs` |
| 铜币拍卖 | `coin_shops`、`coin_sessions`、`coin_lots`、`coin_price_refs`、`coin_fetch_runs`、`coin_api_usage` |
| Numista | `coin_series(_cn)`、`coin_issues(_cn)`、`coin_issue_values` |
| 收藏 | `coin_orders`、`coin_purchase_grades`、`coin_profile_tags`、`coin_collection_series(_members)` |
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
| `RULES.md` | 读取 / 轮次 / 对话 / 输出 / 变更边界 |
| `CHANGELOG.md` | 时间线，只看最上方几条 |

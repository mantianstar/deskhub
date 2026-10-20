# 铜币模块 — 技术方案（当前状态）

> 本文件是 [plan.md](../plan.md) 索引下的**铜币分册**，编号沿用 `§18.x`。
> **跨文件编号**：`§1`~`§6`、`§11`~`§13`、`§16`、`§17`、`§19` → [core.md](./core.md)；`§7`~`§10`、`§14`、`§15` → [knowledge.md](./knowledge.md)。
> **只写「现在怎么做」**——改动过程与时间线见 [CHANGELOG.md](../CHANGELOG.md)，
> 需求见 [spec/coins.md](../spec/coins.md)，任务与验证证据见 [tasks/coins.md](../tasks/coins.md)。
>
> 本章与知识聚合（[core.md](./core.md) / [knowledge.md](./knowledge.md)）是**两套系统**：不共用源、管道、表、页面。
> **共用的只有四样**：同一个 FastAPI 进程、同一个 SQLite 文件、同一个 APScheduler、同一套分层铁律（[core.md](./core.md) §3.2）与文档纪律。

---

## 18.1 关键决策

| 决策点 | 选择 | 理由 | 被否决 |
|---|---|---|---|
| 抓取方式 | **小程序网关 `gapi.mxiqi.com`**（普通 JSON POST） | 实测：免登录、免签名、不走网页那套滑块；比浏览器渲染稳且快得多 | 浏览器渲染 + 人工过滑块（已整体删除） |
| 站点耦合 | 站点接口与字段解析**集中**在 `app/coins/gapi.py` 一处 | 站改版时只改一处，不污染管道与页面 | 解析散落到 pipeline / router |
| 画像抽取 | LLM 抽字段 + 落库可编辑 | spec/coins.md §14.2；理由可校准 | 纯正则（年号/面值形态太杂，维护不住） |
| 表设计 | 独立 `coin_*` 表，不塞进 `items`/`sources` | 数据形态完全不同（专场/拍品 vs RSS 条目），硬塞会让两边都变脏 | 复用 `items` + `module='coins'` |
| 推荐价 | 中位数 + P25–P75 + 样本数；样本 < 3 只给区间 | spec/coins.md §14.6 红线：宁可空着，不给不可验证的数字 | 点估计（不可证伪） |
| 依赖 | 新增 `openpyxl`、`python-multipart` | 读单个 xlsx + 画像页表单 | `pandas`（只读一个表，过重）；`playwright`（已移除） |

---

## 18.2 目录与模块（增量）

```
app/
├── coins/
│   ├── __init__.py
│   ├── profile.py       # orders.xlsx 解析 + LLM 抽字段 + 画像聚合（纯业务，不依赖 FastAPI）
│   ├── gapi.py          # 小程序网关：采集 + 查价的唯一站点入口（免登录、免浏览器）
│   ├── matcher.py       # 画像匹配：拍品名 → 命中标签 + 匹配分
│   ├── pricing.py       # 站内历史成交查询 + 推荐价统计
│   ├── numista.py       # Numista 目录 API：中文→英文 + 系列/单版采集与落库（§18.13）
│   ├── numista_web.py   # Numista 类型页（CDP）：各品相估值 + Frequency + 图片缓存（§18.13.2 / §18.15）
│   ├── translate.py     # 英文→中文内置词表（枚举字段 + 长文本回退；离线免费，§18.13.1）
│   ├── translate_llm.py # 描述/备注/名称的大模型翻译 + 存量重翻（§18.13.4）
│   ├── grades.py        # 历史购买品相录入：年份/面值按 Numista 口径统一 + 组合去重合并（§18.16）
│   └── pipeline.py      # 拍卖管道：采集 → 匹配 → 定价 编排（调度器与 CLI 共用）
├── textmatch.py         # 面值/材质文本匹配（采集过滤与页面筛选共用，§18.13.3 / §18.14）
├── routers/coins.py     # 拍场（/coins、/coins/session/{id}）+ 币库（/coins/info、/coins/series/{id}、/coins/manage）+ 收藏与今日推荐（/coins/collections、/coins/grades、/coins/collection、/coins/explore）
└── templates/coins/     # sessions.html、session.html、info.html、series.html、manage.html、grades.html、collections.html、collection.html、explore.html
config/config.yaml       # coins: 段（见 §18.10）
data/
└── orders.xlsx          # 画像输入（已存在）
```

**分层不变**：SQL 仍只出现在 `app/repository.py`（新增 `coin_*` 读写函数）；`app/coins/*` 不依赖 FastAPI、不依赖 request，因此 `python -m app.cli coins-*` 能跑同一条管道。

---

## 18.3 拍卖：访问方式（小程序网关，**不需要浏览器**）

- 采集（专场 + 拍品）与查价（站内历史成交）都只打 `gapi.mxiqi.com` 的三个接口，**普通 JSON POST**：免登录、免签名、不走 `www.mxiqi.com` 的滑块风控。
- **限速**：请求之间留 `request_interval_seconds`；查价另有更保守的间隔与单轮上限（§18.6）。
- **失败不静默**：接口报错 → 记 `coin_fetch_runs`（`http_error` / `parse_error`），页面标注「数据更新至 X 时间」。
- 站点改版时改一处就行：接口与字段映射都在 `app/coins/gapi.py`。

---

## 18.4 拍卖：画像管道（`app/coins/profile.py`）

1. `openpyxl` 只读模式读 `data/orders.xlsx` → 1567 行（列：订单号 / 店铺名称 / 宝贝名称 / 宝贝价格）
2. 每行「宝贝名称」按 `batch_size` 分批送 LLM，抽 `{country, era, denomination, category, condition}`（复用 `app/llm.py`，prompt `prompt_version = coins-v1`，FakeClient 可注入）
3. 写 `coin_orders`（幂等键 `(order_no, item_name)`）
4. 按维度聚合频次 → `coin_profile_tags`（`weight = 频次`，可后续人工调）
5. **首次需人工校对一遍再启用**（spec/coins.md §14.9）：旧版 `/coins/profile` 编辑页已于 2026-10-19 移除；
   标签仍由 CLI 生成并入库，作为未建立收藏系列时的兜底画像
6. 画像改动次日生效，不追溯改历史推荐（每天管道现取 `enabled=1` 的标签）

---

## 18.5 拍卖：采集管道（`app/coins/gapi.py` + `pipeline.py`）

- **专场 + 拍品都走小程序网关**：
  1. `POST miniprogram.auction.ListAuction`，body `{"auction_mode":1}`（实时）/ `{"auction_mode":2}`（限时）
     → 当前专场列表（`name` / `start_time` / **`end_time`（=截拍时间）** / `num` / `status` / 店铺 / 围观）；
  2. 逐场 `POST miniprogram.auction.get`，body `{"auction_id":<id>}`
     → 专场详情 + 店铺 + **全部拍品**（`auction_items.items`，一次给全）。
- **「拍卖时间」按站内口径存 `end_time`（截拍时间）**：`coin_sessions.auction_at` 存 UTC ISO，`auction_at_raw` 存文案（如「10月12日 星期一 19:30」）。列表页那张卡显示的也是 `end_time`。
- 价格字段是**分**（`price_deposit=2000` → ¥20）；图片是 OSS key，用响应里的 `cdn.oss` 拼前缀。
- 以麦稀奇 id 幂等 upsert（专场、拍品各一个唯一键）；单场失败只记日志、不中断其余专场。
- 查价（§18.6）同走 `miniprogram.home.filter` —— 不需要浏览器与登录。

---

## 18.6 拍卖：匹配与定价（`matcher.py` / `pricing.py`）

- **匹配**：拍品名做标签命中（国家 / 年代 / 面值 / 品类，先做修饰词归一，如去掉「包老保真」「美品」「极美品」）；`match_score = Σ 命中标签的 weight`；`>= coins.match_threshold` 才置 `is_recommended=1`。
- **定价**：
  1. 关键词 = 命中标签里最强的「国家 + 年代 + 面值/品类」组合（spec/coins.md §14.6）
  2. `POST /miniprogram.home.filter`，body `{"search_params": {"keywords": <关键词>, "status": 2}}`（空格多词也认，免登录）
  3. 逐条取 `price`（分）与 `end_time`（成交时间）→ `median` / `p25` / `p75` / `sample_count`，写 `coin_price_refs`
  4. 样本 < `min_samples` → 只给区间 + 标注「样本不足」；0 样本 → 标注「无成交样本」，不写价格点估计
  5. **同一关键词当日缓存**，避免重复请求（限速）
- **报价前的两道闸**：
  1. **必须有「面值/品类」命中**（`matcher.SPECIFIC_DIMENSIONS`）：只有国家/年代的命中拼出来的关键词太泛（实测「巴拿马 1967」把当前价 ¥330 的精制银套币报成 ¥2），没有具体维度就**主动不报价**（`lots_pricing_too_generic`）。
  2. **数字边界**：面值/年代这类以数字开头或结尾的值不许咬进更长的数字（「5分」≠「25分」），否则关键词会整个错位。
- **查价限速**：`price.query_interval_seconds`（默认 15，带 ±40% 抖动，避免固定节奏）+ `price.max_queries_per_run`（默认 20，超出的推迟到下一轮）。
- **专场全量入库，列表页过滤**：`coin_sessions` / `coin_lots` 把抓到的都落库（未命中的也落），`list_coin_session_cards()` 只返回「命中 ≥1 条推荐拍品」的场。这样画像改动能立刻重算出新的推荐，**不必重抓**。

---

## 18.7 拍卖：数据层（DDL，`app/models.py` 增量；`schema_version` 1 → 2）

> 全局搜索索引 `coin_lot_fts`（FTS5）随 `schema_version` 11 → 12 新增：存量拍品在 `app/migrations/v12.py` 回填，之后由 `upsert_coin_lot` 同步、`coin_lots` 删除触发器清理。

```sql
CREATE TABLE IF NOT EXISTS coin_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no TEXT NOT NULL, shop_name TEXT, item_name TEXT NOT NULL, price REAL,
  country TEXT, era TEXT, denomination TEXT, category TEXT, condition TEXT,
  parsed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_coin_orders ON coin_orders(order_no, item_name);

CREATE TABLE IF NOT EXISTS coin_profile_tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dimension TEXT NOT NULL CHECK (dimension IN
    ('country','era','denomination','category','condition','shop','price_band')),
  value TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1,
  hit_count INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  note TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_coin_tags ON coin_profile_tags(dimension, value);

CREATE TABLE IF NOT EXISTS coin_shops (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mxiqi_uid TEXT NOT NULL UNIQUE, name TEXT, avatar_url TEXT,
  badges TEXT,            -- JSON 数组：[{"kind":"certified"},{"kind":"years","n":2},{"kind":"deposit"}]
  fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS coin_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mxiqi_id TEXT NOT NULL UNIQUE,
  shop_id INTEGER REFERENCES coin_shops(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  status TEXT,            -- preview | running | closed
  auction_at TEXT,        -- UTC ISO8601
  auction_at_raw TEXT,    -- 站点原始文案（如「10月8日 星期四 15:25」）
  deposit REAL, lot_total INTEGER, view_count INTEGER,
  cover_urls TEXT,        -- JSON 数组（列表页 4 图）
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coin_lots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES coin_sessions(id) ON DELETE CASCADE,
  mxiqi_id TEXT NOT NULL UNIQUE,
  lot_no TEXT, title TEXT NOT NULL,
  start_price REAL, current_price REAL,
  image_urls TEXT,        -- JSON 数组（页面展示 ≤3）
  tags TEXT,              -- JSON：{"发行年份":"1808","国家地区":"印度"}
  match_score REAL NOT NULL DEFAULT 0,
  is_recommended INTEGER NOT NULL DEFAULT 0,
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_lots_rec ON coin_lots(session_id, is_recommended, match_score DESC);

CREATE TABLE IF NOT EXISTS coin_price_refs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lot_id INTEGER NOT NULL REFERENCES coin_lots(id) ON DELETE CASCADE,
  keyword TEXT NOT NULL, sample_count INTEGER NOT NULL,
  p25 REAL, median REAL, p75 REAL,
  raw TEXT,               -- JSON：命中的成交（标题 + 价格）
  queried_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_refs_lot ON coin_price_refs(lot_id);

CREATE TABLE IF NOT EXISTS coin_fetch_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,     -- sessions | session_detail | price_search
  target TEXT,            -- 专场 id / 关键词
  started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL,   -- ok | http_error | parse_error | timeout
  http_status INTEGER, items_new INTEGER DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_coin_runs ON coin_fetch_runs(kind, started_at DESC);
```

**repository 增量函数**（`app/repository.py`）：
`upsert_coin_order` / `count_coin_orders` / `list_coin_orders`、
`list_coin_profile_tags(only_enabled)` / `upsert_coin_tag(dimension, value, weight, hit_count)` /
`add_coin_tag` / `set_coin_tag_enabled` / `set_coin_tag_weight` / `delete_coin_tag` / `get_coin_tag`、
`upsert_coin_shop` / `upsert_coin_session` / `upsert_coin_lot` / `update_coin_lot_match` / `list_coin_lots_for_matching`、
`upsert_coin_price_ref` / `get_coin_price_ref`、
`record_coin_fetch_run` / `latest_coin_fetch_runs` / `coin_fetch_summary`、
`list_coin_session_cards` / `get_coin_session_card` / `get_coin_session_detail` /
`count_coin_sessions` / `count_coin_lots` / `count_recommended_lots` /
`set_coin_shop_followed`。

---

## 18.8 拍卖：路由与页面

| 路由 | 方法 | 职责 | 模板 |
|---|---|---|---|
| `/coins` | GET | 拍场：专场列表（按拍卖截止日期、推荐状态 全部/已推荐/未推荐、推荐类型、关注状态 全部/已关注/未关注筛选；默认今日 + 已推荐；排序 精准 > 相似 > 已关注 > 普通，同层按截拍时间；12 个/页；空态提示） | `coins/sessions.html` |
| `/coins/session/{id}` | GET | 拍场详情：店铺头（关注按钮）+ 拍品分「精准推荐 / 相似推荐 / 其他拍品」三组（推荐项带推荐原因，其他拍品 20 件/页；id 不存在返回 404）；带 `lot={拍品 id}` 时若命中「其他拍品」则直接渲染该拍品所在页；每条拍品带 `id="coin-lot-{拍品 id}"` 锚点 | `coins/session.html` |
| `/coins/search` | GET | 全局拍品搜索：FTS5 关键词（拍品名 / 年份 / 国家 / 材质）+ 价格区间；只取 `auction_at >=` 业务时区今日零点起、且状态 running / preview；排序 精准 > 相似 > 关注店铺 > 普通，同层按截拍时间；20 件/页；拍品卡链接 `/coins/session/{session_id}?lot={lot_id}#coin-lot-{lot_id}` | `coins/search.html` |
| `/coins/shop/{id}/follow` | POST | 关注 / 取消关注店铺（改 `coin_shops.followed`，302 回 `next`，只接受铜币模块内地址） | — |
| `/coins/refresh` | POST | 手工触发一轮采集 + 查价（后台任务，302 回 `/coins`） | — |

**性能**

- `translate.to_zh` 与 `textmatch.normalize` 都是纯函数，统一加 `lru_cache`：当日 2800+ 拍品、
  13 个收藏系列时，推荐加载从约 4s 降到约 0.6s（暖缓存约 0.2s）。
- 面值音译差异（`莱普塔` / `雷普塔`）由 `denomination_variants` 双向展开，避免相似推荐与右侧拍场脱节。
- 全局搜索走 FTS5 索引 `coin_lot_fts`：`textmatch.fts_text` 写索引（CJK 逐字分词），`textmatch.fts_query` 拼 `MATCH`；先取 300 条候选，再在 `app/coins/search.py` 套推荐标签排序，避免拍品 / 专场全量联查。
- 关键词含 CJK 时整词按**相邻短语**匹配（`1便士` 不命中 `6便士`），纯数字精确匹配、纯字母前缀匹配。

**模板约定**

- 视觉参考 `data/shop.png`（专场卡：拍卖时间 / 专场名 / 店铺头像 / 4 图 / 拍品数 / 围观数）与 `data/item.png`（店铺头 + 拍品卡）。
- 「推荐价」区块必须同时显示**区间 + 样本数 + 关键词**，可点开看命中的成交明细（`coin_price_refs.raw`），对应 spec/coins.md §14.6「可追溯」。
- 沿用 [knowledge.md](./knowledge.md) §10 前端约定：Jinja2 + 少量原生 JS、无构建链、无 CDN。
- 导航栏「铜币」为正式入口（`base.html`），指向 `/coins/collection`（今日推荐）；`/coins` 仍是拍场。
- 铜币模块内统一使用 `coins/_macros.html` 的 5 个一级入口：今日推荐 / 收藏 / 币库 / 候选池 / 拍场。
- 旧版标签画像页面 `/coins/profile` 已移除，`coin_profile_tags` 与兜底匹配保留。

---

## 18.9 调度与 CLI

| job | 触发 | 说明 |
|---|---|---|
| `daily_coins` | 每天 `08:00` Asia/Shanghai | 与 `daily_pipeline` 同批但**独立任务**；`coalesce=True`、`misfire_grace_time=3600`、`max_instances=1` |

- 采集与查价**各有一个开关**（`/control`，见 [core.md](./core.md) §19）；两个都关时这一轮直接跳过。
- 两个 `daily_*` 任务**互不传播失败**（spec/coins.md §14.9）：铜币采集挂掉不影响知识日报，反之亦然。
- **不做启动补拉**（重启不抓取 / 不洗牌），见 [core.md](./core.md) §12 与 §19。

| CLI 命令 | 用途 |
|---|---|
| `coins-parse-orders` | 解析 `orders.xlsx` → 画像入库（调 LLM） |
| `coins-rebuild-profile` | 只用已入库的 `coin_orders` 重算画像（不调 LLM） |
| `coins-tags` | 打印画像标签表格（同 `cli sources` 风格） |
| `coins-fetch [--limit N]` | 只采集专场 + 拍品 |
| `coins-price` | 只对已入库的推荐拍品查价 |
| `coins-pipeline [--limit N]` | 采集 → 匹配 → 定价 全流程 |
| `coins-series --country --year --denomination [--composition]` | 按国家/年份/面值查 Numista 系列并落库（§18.13） |
| `coins-issues --series-id` | 按系列号取该系列全部单版并落库（§18.13） |
| `coins-values --series-id [--cdp URL]` | 类型页取各品相估值 + Frequency（§18.13.2） |
| `coins-images --series-id [--cdp URL]` | 下载系列正 / 反图到本地（§18.15） |
| `coins-translate [--series-id N]` | 读英文表重翻中文表（不重采、不耗额度，§18.13.4） |

---

## 18.10 配置（`config/config.yaml` 增量）

```yaml
coins:
  enabled: true
  orders_xlsx: data/orders.xlsx
  gapi_base_url: https://gapi.mxiqi.com  # 采集 + 查价的唯一来源（免登录、免浏览器）
  request_interval_seconds: 3            # 限速
  max_sessions_per_run: 80               # 单轮采集上限
  match_threshold: 1.5                   # 匹配分阈值（spec/coins.md §14.11 待校准）
  price:
    status: 成交                         # 只用成交样本
    window_days: 180
    min_samples: 3                       # 少于 3 条不给单一价
    statistic: median                    # median | mean
    query_interval_seconds: 15           # 查价间隔（带 ±40% 抖动）
    max_queries_per_run: 20              # 单轮查价上限，超出的推迟到下一轮
  profile_llm:
    prompt_version: coins-v1
    batch_size: 20                       # 每批送模型的订单数
    concurrency: 6                       # 抽取批次并发
  numista:                               # 铜币参考库（§18.13）
    base_url: https://api.numista.com/v3
    api_key_env: DESKHUB_NUMISTA_API_KEY  # 只写变量名，真实值放 .env
    request_interval_seconds: 2          # 请求间隔（接口额度有限）
    max_series: 10                       # 单次搜索最多取几个系列的详情（Type Detail 有配额）
    timeout_seconds: 20
    monthly_limit: 1500                  # 每月请求上限（免费额度 2000，留 25% 余量）
    max_requests_per_run: 40             # 单轮请求上限
    cdp_url: http://127.0.0.1:9222       # 类型页 / 取图的 CDP 端点（§18.13.2 / §18.15）
```

**另外新增 `llm.timeout_seconds`（全局段）**：实测 30s 会把大多数抽取请求打成 `ReadTimeout`（每批 20 条、输出较长），故把 LLM 单次请求超时做成配置，默认 120s。`get_client()` 统一从这里取，打分与画像共用。

`.gitignore` 不需要为铜币模块新增条目。

---

## 18.11 拍卖：测试策略（沿用「测试不发真实网络请求」）

| 用例 | 断言 |
|---|---|
| 解析 `orders.xlsx`（离线 fixture） | 用一份小 xlsx（含中/英/年号混合名）断言逐行读出与字段抽取形状；LLM 走 FakeClient |
| 画像聚合 | 频次 → `weight` 正确；`(dimension, value)` 唯一；重复导入不增行 |
| 站点解析（离线 payload） | 喂 **gapi 真实响应字段**（专场列表 / 详情+拍品 / 成交）驱动 `gapi.py` 的纯函数，断言 id、截拍时间、价格（分→元）、封面图、店铺徽标、`status` 映射 |
| 请求参数 | 断言 `list_sessions` 两个 mode 都请求、按 id 去重、滤掉公示；`search_sold` 的 body 是 `{"search_params":{"keywords":…,"status":2}}`；空关键词不发请求 |
| 匹配 | 修饰词归一（「极美品 X」命中 X）、无命中不推荐、多命中按权重累加 |
| 定价统计 | 样本 5 条 → 中位数/分位正确；样本 2 条 → 只给区间且标「样本不足」；0 条 → 不写点估计 |
| 页面 | `TestClient` 渲染 `/coins`（空态 + 有数据两态）、`/coins/session/{id}`（店铺头字段齐全）；旧版 `/coins/profile` 已移除，断言 404 |

> 画像的 LLM、麦稀奇的网络都不进测试；两者都用 fixture / FakeClient 挡在最外层。

---

## 18.12 里程碑

拍卖模块的里程碑为 M7~M10；参考库与页面为 M11~M22。任务级拆解与验证证据落在 [tasks/coins.md](../tasks/coins.md)。

---

## 18.13 铜币参考库（Numista；对应 spec/coins.md §14.13）

**定位**：这是「硬币本身」的目录库，与 §18.3 的麦稀奇（拍卖）**两条独立管道**。不共用表、不共用客户端。

- **模块**：`app/coins/numista.py`：站点解析**纯函数** + 中文→英文映射 + `NumistaClient`（薄封装）+ 采集编排（落库）。与 `gapi.py` 一样「解析只吃 dict」，便于离线单测；中文映射与站点字段集中在这一处，改版只改这一处。
- **中文→英文**：`ISSUER_CODES`（中文国家 → Numista **本国** code，如 英国=`royaume-uni`）、`DENOMINATION_UNITS`（面值单位 → 英文）+ 数字前缀。**不调 LLM**；未收录的国家 `NumistaError`（明确报错，不猜）。也接受已是对应 code 的输入（绕过映射）。
- **站点事实（实测，务必按此避免踩坑）**：
  - 搜索 `GET /types`：参数 `q` + `issuer` + **`year`（单年）** + `category`；`min_year`/`max_year` 会被**忽略**。返回 `{"count", "types":[...]}`，每项是**轻量**卡（id/title/object_type/issuer/min_year/max_year/缩略图）。
  - 系列详情 `GET /types/{id}`：全字段（value.text、size、weight、thickness、shape、composition、obverse/reverse 的 description/lettering/picture、references → 目录号、ruler）。
  - 单版 `GET /types/{id}/issues`：**独立端点，返回裸数组**（不是 `/types/{id}?includeIssues=1`）。字段仅 `id/year/gregorian_year/mintage/comment/mint_letter/references/marks`。
  - 发行方 code 是法文式 slug；`united-kingdom`（L1）含百慕大等属地，本国是 `royaume-uni`（L2）。
- **字段缺口**（API 没给的不臆造）：单版无「造币厂名 / 版别备注 / 重量 / 厚度 / 币边 / 是否精制·样币 / 图片」→ 尺寸/重量/厚度/币边**继承系列层**；厂标=`mint_letter`；精制/样币按 `comment` 关键词（proof / essai / pattern）标注，无备注则留 `NULL`（未知≠否）；其余留空。
- **数据层（§18.7 增量，`schema_version` 2 → 3）**：新增两张表（DDL 见 `app/models.py`）——
  `coin_series`（numista_id 唯一、title/url/object_type/issuer/min-max_year/ruler/denomination/size/weight/thickness/shape/composition/edge/正反面描述·铭文·图片/catalog_no/fetched_at）、
  `coin_issues`（numista_id 唯一、series_id 外键、year/gregorian_year/mint/mint_mark/variety/comment/mintage/size/weight/thickness/edge/is_proof/is_essai/picture/fetched_at）。
  repository 增量：`upsert_coin_series` / `upsert_coin_issue`（按 numista_id 幂等） / `get_coin_series` / `list_coin_series` / `list_coin_issues` / `count_coin_series` / `count_coin_issues`。
- **访问边界**：只打 Numista 公开 API（带 Key，走 `DESKHUB_NUMISTA_API_KEY`）；`NumistaClient._get` 内置请求间隔限速；单次搜索最多取 `coins.numista.max_series` 个系列的详情（Type Detail 有配额）。
- **测试**（沿用「测试不发真实网络请求」）：`tests/test_coins_numista.py` —— 翻译用例、真实 payload 驱动的解析用例、桩客户端验请求路径与参数、桩客户端 + 真库验幂等落库。

### 18.13.1 目录号 / 中文表 / 加列 / 风控

1. **目录号取全**：`references` 里**每一条**都取，写成 `目录代码#编号` 逗号分隔（如 `KM#790,Sp#3961`），不只取 KM。系列与单版都适用（单版 `catalog_no` 是新增列）。
2. **列扩充**（`schema_version` 3 → 4，登记在 `app/migrations/v4.py`）：`coin_series` 加 `comments`（type 的 `comments`）与 `mints`（造币厂名 JSON 数组）；`coin_issues` 加 `catalog_no`。
3. **中文表**：新增 `coin_series_cn` / `coin_issues_cn`，**列数与英文表完全一致**，只把可翻译字段注成中文（系列：title/object_type/issuer/ruler/denomination/shape/composition/edge/obverse_desc/reverse_desc/comments/mints；单版：mint/comment/edge），其余字段原样复制。落库时英文表与中文表**各写一份**（`_store_series` / `_store_issues`），按 numista_id 幂等。
   - 翻译实现：`app/coins/translate.py` 的 `GLOSSARY`（英文→中文）按**词长降序**做**整词**替换（前后不挨字母才算命中，避免 `Mint` 咬进 `Mints`/`Mintmark`）。枚举类字段能精确成中文；**说明类长文本**做术语替换，未收录片段**保留英文原文**（不做假翻译）。**离线、免费、零依赖**。
   - **枚举类字段**继续走这里；**描述 / 备注 / 名称**改由大模型翻译（§18.13.4），本模块对长文本退化为「词表结果」+「大模型不可用时的回退」。
4. **风控**（免费额度每月 2000 次，响应**不带限流头**）：`NumistaClient._guard_quota` 每次请求前检查 —— 单轮上限 `max_requests_per_run`（默认 40）、月度上限 `monthly_limit`（默认 1500，按业务时区切月）；每次成功请求落 `coin_api_usage` 计数，CLI 末尾打印「本月已用 N/1500」。

- **repository 增量**：`upsert_coin_series_cn` / `upsert_coin_issue_cn`、`get_coin_series(cn)` / `list_coin_series(cn)` / `list_coin_issues(cn)` / `count_coin_series(cn)` / `count_coin_issues(cn)`、`record_api_call` / `count_api_calls_since`、`local_month_start_utc`。
- **字段新增链路**（单版 / 系列加字段，按此顺序一遍改完，不逐层探索）：`models.py` 的 DDL + dataclass → `SCHEMA_VERSION` + `app/migrations/vN.py`（老表加列）→ `IssueInfo` / `series_from_type` 解析 → `repository` 的 `_*_COLUMNS` / `_*_values` / `_row_to_*` → 模板 → 测试构造器与断言 → CHANGELOG。
- **迁移机制**：见 [core.md](./core.md) §6.2（`CREATE TABLE IF NOT EXISTS` 只能新增表；加列走 `app/migrations/` 登记的幂等 `ALTER TABLE`）。

### 18.13.2 各品相估值 + Frequency（网页链路）

**目标**：把类型页表格里「每个年份（含版别）× 每个品相」的估值与 Frequency 拿下来。

- **为什么是网页而不是接口**：`/types/{id}` 与 `/types/{id}/issues` 的字段里**都没有**这两项（Frequency 的定义甚至只写在页面上）。所以新开一条**网页表格**链路：`en.numista.com/{type_id}`（路径走 `coins.numista.cdp_url`）。
- **模块**：`app/coins/numista_web.py` —— `parse_type_page`（**纯函数**，只吃 HTML）+ `fetch_type_page`（CDP 取页）+ `collect_issue_values`（落库编排）。与 `numista.py`（API）并列、互不依赖。
- **取页（本块最容易踩的地方，实测穷举过）**：
  - `curl` / `httpx`（带完整浏览器头）/ `curl_cffi`（chrome、safari 指纹伪装）→ **全部 403**，返回 Cloudflare 的 `Checking connection…`（HTML 里有 `challenge-platform`）。
  - Playwright `launch`（headless 与 headful 都试）→ 卡在同一个校验页。
  - `open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir=<临时 profile>` + Playwright `connect_over_cdp` → **成功**。
  - 结论：**只用 `connect_over_cdp`**，绝不 `launch`；也因此**不需要** `playwright install`（用的是用户自己的 Chrome）。地址取 `coins.numista.cdp_url`（默认 `http://127.0.0.1:9222`），CLI 可用 `--cdp` 覆盖。
  - 取页后等表格出现：`wait_for_selector("tr.date_row")`（超时则读一次页面内容并提示「页面停在 Cloudflare 校验页」）。**不轮询 `page.content()`** —— 校验页会自我刷新，导航中读内容会抛「page is navigating and changing the content」。用完 `page.close()`，**不关用户的浏览器**。
- **解析（服务端渲染，结构稳定）**：
  - 行：`<tr class="date_row">`；估值：`<td class="value" data-grade="G|VG|F|VF|XF|AU|UNC">`（空格即该品相没值 → `NULL`）；
  - Frequency：`<td class="own_stat"><span class="cluster*">27%</span>`；
  - **单版编号**：每行**后面**紧跟 `<tbody id="collec_line{单版编号}">` —— 取这个编号作为行主键，正好与 API 的 issue id 一一对应（实测系列 670 两边 15 个编号完全一致、顺序也一致，落库后 `coin_issue_values` 与 `coin_issues` 对不上的是 0 行）。
  - 编号 0 的是「Undetermined」占位模板（页面里 `display:none`）→ 跳过。
  - **币种**：表下 `Values in the table above are expressed in <strong>CNY</strong>` → 读出来存进 `currency`（不写死；页面按访客币种渲染）。
- **数据层（`schema_version` 4 → 5，纯新增表 → 登记进 `db.FORWARD_ADDITIVE_UPGRADES`，无需 ALTER）**：`coin_issue_values`（`numista_id` 唯一 = 单版编号、`series_numista_id`、`currency`、`value_g…value_unc`、`frequency`、`fetched_at`）。**不设外键**：页面与接口是两条链路，谁先采到都该存下来，用 `numista_id` 对齐即可（落库后 join `coin_issues` 校验过：0 行对不上）。
- **repository 增量**：`upsert_coin_issue_values`（按 `numista_id` 幂等）/ `get_coin_issue_values` / `list_coin_issue_values(series_numista_id)` / `count_coin_issue_values`。
- **CLI**：`coins-values --series-id 670 [--cdp URL]` → 打印「估值币种 + 单版编号 / 年份 / G…UNC / Frequency」表，末尾给入库行数与文件表总行数；**不耗 Numista 接口额度**。
- **不做无人值守调度**：这条链路要求本机开着带调试端口的 Chrome，只在**手工按需**跑（Frequency 变化很慢，一次采长期可用）。
- **测试**（沿用「测试不发真实网络请求」）：`tests/test_coins_numista_web.py` + 真实页面切片 fixture `tests/fixtures/numista_type_670.html`（原样截取）—— 验币种、15 行、编号与 API 对齐、各品相数值、空值留空、占位行跳过，以及桩掉取页后的幂等落库；另有一条「挂不上浏览器 → 提示怎么开 Chrome」的报错路径用例。

### 18.13.3 面值 / 材质精确过滤 + 清理存量

**目标**：采集时把「多余的系列」挡在库外。

- **API 事实（打真实接口实测确认）**：`/types` 的 `q` 只做**模糊匹配** —— 查 `1 penny` 会连 `½ Penny` 一起返回；且**没有面值筛选参数**：`denomination=1` / `value=1` / `currency=1` 传了都返回同一结果、**不报错**（静默忽略）。只有 `material=<id>` 能按材质筛（`/materials` 给 71 个材质及 id），但要维护「中文材质 → id」映射，**未采用**。
- **做法**：`collect_series` 由「搜到就落库」改为「搜到 → 逐个取详情 → 按面值 / 材质过滤 → 只落库匹配的」。过滤用详情里的 `value.text`（面值）与 `composition.text`（材质）：**英文关键词 vs 英文值**，再**用户原文 vs `translate.to_zh(值)`**（`½ Penny` →「半 便士」，用户的「半便士」才对得上）。面值必筛；材质非空才筛。被挡掉的打日志。
- **共用一份文本匹配**：`app/textmatch.py`（`normalize` = 去空白 + 转小写 + `½→half` 等分数符号展开；`contains` = 带数字边界），`repository.search_coin_series`（页面本地筛选）与 `collect_series`（采集过滤）**同一口径**，不分叉。
- **入口**：管理页「① 获取系列」把 `composition` 一并传下去（`fetch_series` → `collect_series`）；CLI `coins-series` 加 `--composition`（可空）。
- **清理存量**：`repository.delete_coin_series(numista_id)` —— 删中英两张系列表（`coin_issues[_cn]` 靠 `ON DELETE CASCADE` 连带）+ 显式删 `coin_issue_values`。
- **测试**：`tests/test_coins_numista.py` 覆盖「1便士挡掉 ½ Penny」「+青铜挡掉银币」「半便士命中 ½ Penny」「textmatch 归一化 / 数字边界」「delete 连带删单版 + 估值」。

### 18.13.4 描述 / 备注 / 名称走大模型翻译

**目标**：把**自由长文本**的中文质量提上来。词表只能做术语替换，长句会中英混排。

- **分工不变的部分**：枚举类字段（`object_type / issuer / ruler / denomination / shape / composition / edge / mints / mint`）**继续走词表** —— 它是「按面值 / 材质过滤」的依据，要确定性。
- **新模块**：`app/coins/translate_llm.py`。管的字段：系列 `title / obverse_desc / reverse_desc / comments`、单版 `comment`。解析是**纯函数**（`parse_series_texts` / `parse_issue_comments`，容忍 ```json 围栏与部分缺失）。
- **批量化**：一个系列的长文本**一次**请求；该系列**全部单版备注一次**请求（按 `id` 回填）—— 避免「63 个单版 = 63 次调用」。
- **降级**：没配 `DESKHUB_LLM_API_KEY` 或调用 / 解析失败 → **回退内置词表结果**，只记 warning，采集不中断。落点：`numista._translator`（拿不到 Key 就给 `None`）+ `_cn_series_info` / `_cn_issue_infos`（逐项 try，失败保留词表译文）。
- **注入**：`collect_series` / `collect_issues` 新增 `llm_client` 参数（测试注入 `llm.FakeClient`，不发真实网络）；不传则 `llm.get_client(config)`，自建的用完 `aclose`。
- **剥 HTML**：Numista 的 `comments` 是 HTML 片段（`<p>` / `<br>` / `<i>` / `<strong>`），页面用 Jinja **自动转义**渲染 → 不剥就会把标签显示成字面文本。故 `translate.strip_html()`（块级 / 换行标签换空格，避免词粘连；实体解码；空白归一）+ `strip_html_texts()`（只动对象上存在的字段），在**中文表落库前**统一调用：① `numista._cn_series_info` / `_cn_issue_infos`；② `translate_llm._retranslate_one`。**英文表保留原文**。
- **存量重翻**：`translate_llm.retranslate(config, *, series_id=None, llm_client=None)` + CLI `coins-translate [--series-id N]` —— 读**英文表**做输入、覆盖写**中文表**，**不重采 Numista、不耗额度**；单版行沿用原 `fetched_at`（没重采就不伪造抓取时间）。无 Key 时抛 `llm.LLMError`（CLI 打印「错误：…」并 exit 1）。
- **测试**：`tests/test_coins_translate.py` —— 解析纯函数（围栏 / 部分缺失 / 空则不发请求）、`FakeClient` 驱动的取数、落库走大模型 / 失败回退词表 / 没 Key 只用词表、`strip_html` 纯函数与「中文表无标签、英文表留原文」、`retranslate` 覆盖中文表且不加行、无 Key 报错、`--series-id` 限定范围。另在 `tests/conftest.py` 加 **autouse** 夹具，保证全量测试**一律不发真实 LLM 请求**。
  - **夹具要点**：必须把 Key **置空**（`setenv(..., "")`）而不是 `delenv` —— `config.load()` 会 `load_dotenv(".env", override=False)`，变量**不存在**时 .env 里的真实 Key 会被读回来；**存在但为空串**时 dotenv 不覆盖。

---

## 18.14 铜币信息页面（Web 浏览 + 采集触发；对应 spec/coins.md §14.14）

**定位**：§18.13 参考库的**页面层**。SQL 只在 repository；路由只做「参数校验 + 调 repository / numista + 渲染」；采集**零改动复用** §18.13 的 `collect_series` / `collect_issues` / `collect_issue_values` / `download_series_images`（与 CLI 同一函数）。

**路由**（追加到 `app/routers/coins.py`）

| 方法 | 路径 | 职责 | 同步 / 异步 | 模板 |
|---|---|---|---|---|
| GET | `/coins/info` | 查询页：国家 + 年份 + 面值 + 材质（均可选）→ 本地系列列表 | 同步 | `coins/info.html` |
| GET | `/coins/series/{numista_id}` | 系列详情：系列信息 + **合并后的「各年份信息」表** + ②③④ 补数据动作条；未采纳候选用候选 payload 渲染并给采纳横幅；不存在 404 | 同步 | `coins/series.html` |
| GET | `/coins/manage` | 管理页：两步表单 + 结果列表（结果按面值 / 材质本地筛选，含每系列已采「年份数 · 版别数」）+ 可选错误提示 | 同步 | `coins/manage.html` |
| POST | `/coins/manage/fetch-series` | 按钮 1：`collect_series(country, year, denomination, composition)`——取详情后按**面值 / 材质精确过滤**（见 §18.13.3） | 同步 `await` | 302 → `/coins/manage?...` |
| POST | `/coins/manage/fetch-issues` | 按钮 2：`collect_issues(series_id)`；可选 `return_to` 回跳 | 同步 `await` | 302 → `return_to` 或 `/coins/manage?...` |
| POST | `/coins/manage/fetch-values` | 按钮 3：`numista_web.collect_issue_values(series_id)`（类型页 / CDP）；可选 `return_to` | 同步 `await` | 302 → `return_to` 或 `/coins/manage?...` |
| POST | `/coins/manage/download-images` | 按钮 4：`numista_web.download_series_images(numista_id=series_id)`（CDP 下载）；可选 `return_to` | 同步 `await` | 302 → `return_to` 或 `/coins/manage?...` |

**为什么按钮 3 / 4 也同步 + 就地提示**：它们**不走 Numista 接口、走类型页**（需用户自己开的调试端口 Chrome，见 §18.13.2 / §18.15），失败抛 `NumistaPageError`（`_cdp_hint` 已带「怎么开 Chrome」的指引）—— 与 `NumistaError` 一样用 `try/except` 就地渲染成页面提示，不吞进日志。

**为什么两组按钮都同步**（与 §18.8 `/coins/refresh` 的异步取舍相反）

- `collect_series` 最多 1 次搜索 + `max_series`（默认 10）次详情 ≈ 11 个请求，按 2s 限速约 20s；`collect_issues` 只有 2 个请求。本地单用户可接受浏览器等待。
- 同步才能把 `NumistaError`（未收录国家 / Key 未配置 / 月度额度用尽）**直接渲染成页面提示**；后台任务会把它吞进日志，用户看不到（`/coins/refresh` 是被迫异步：一轮要拉几十个专场、可能几分钟）。
- 同步让「取系列 → 取年份」两步**无缝衔接**，不必刷新页面。

**repository 增量**（`app/repository.py`；SQL 只在此处）

```python
def list_coin_series_countries(*, cn: bool = False) -> list[str]:
    """国家候选：SELECT DISTINCT issuer FROM coin_series[_cn] WHERE issuer IS NOT NULL ORDER BY issuer"""

def list_coin_series_denominations(*, cn: bool = False) -> list[str]:
    """面值候选：SELECT DISTINCT denomination ...（同理）"""

def list_coin_series_compositions(*, cn: bool = False) -> list[str]:
    """材质候选：SELECT DISTINCT composition ...（同理）"""

def search_coin_series(
    *,
    issuer: str | None = None,
    year: int | None = None,
    denomination: str | None = None,
    composition: str | None = None,
    cn: bool = False,
) -> list[CoinSeries]:
    """按条件查系列（year 落在 [min_year, max_year] 即命中）；四参数可空（全空=全部）。
    国家 / 年份走参数化 SQL；面值 / 材质在取回后经 `_text_hit` 做**归一化宽松匹配**。
    ORDER BY issuer, min_year, numista_id。"""

def coin_series_issue_stats(*, cn: bool = False) -> dict[int, CoinSeriesIssueStats]:
    """{系列 numista_id: (不同年份数, 版别数)}；LEFT JOIN coin_issues[_cn]，
    年份数 COUNT(DISTINCT i.year)、版别数 COUNT(i.id)，一次查询，供列表显示「N 个年份 · M 个版别」。"""

def coin_series_value_counts() -> dict[int, int]:
    """{系列 numista_id: 已采集估值条数}；coin_issue_values 按 series_numista_id 聚合，
    供管理页显示「估值 已采 N 条 / 未采」。"""
```

> **年份统计口径**：`coin_series_issue_stats` 同时返回「不同年份数」与「版别数」两个值 —— 一个年份可有多个版别（厂标 / 变体 / 精制），二者不是一回事（实测系列 670：1895–1901 应是 **7 个年份 · 15 个版别**）。`CoinSeriesIssueStats` 定义在 `app/models.py`。

> **面值 / 材质为什么不在 SQL 里筛**：库值是词表翻译后的结果，形如 `1 便士` / `半 便士`（数字与单位间带空格），用户习惯输入 `1便士` —— 需要**归一化掉空白**再匹配；且关键词以数字开头 / 结尾时不许咬进更长的数字（`1便士` ≠ `11便士`）。这两件事 SQL 表达不了，故 `search_coin_series` 先用 SQL 收窄国家 / 年份，再在 Python 里用 `textmatch.contains` 过滤面值 / 材质。查询页只是**本地筛选**；**采集时**的过滤另见 §18.13.3（同一个 `textmatch`，不分叉）。

**详情页聚合**（路由内组合现有函数，不新增视图模型）

```python
series = repository.get_coin_series(numista_id, cn=True)      # cn=True：只用中文表
issues = repository.list_coin_issues(series.id, cn=True)
values = {v.numista_id: v for v in repository.list_coin_issue_values(numista_id)}
rows = [(issue, values.get(issue.numista_id)) for issue in issues]  # 一行一个版别，估值按 numista_id 对齐
year_total = len({i.year for i in issues if i.year is not None})    # 不同年份数（≠ 版别数）
```

> 年份信息与估值**合并为一张「各年份信息」表**：以 `coin_issues` 的行为主，同一行的各品相估值从 `coin_issue_values` 取（按 `numista_id` 对齐）；`coin_issue_values` 里多出的、`coin_issues` 没有的编号不单列。
>
> **合并后列**：`年份 | 厂标 | 发行量 | G | VG | F | VF | XF | AU | UNC | 目录号 | 备注 | 稀有度` —— 以估值表字段为准，插入 `厂标`（年份后）、`目录号`（UNC 后）、`备注`（目录号后）。「发行量 / 厂标 / 目录号 / 备注」取该行单版（`coin_issues`）的 `mintage` / `mint_mark` / `catalog_no` / `comment`。该品相无估值时格子留空。**估值为空时该表照常渲染**（只给年份信息）并附「估值未采集」提示。

**管理页错误与校验**

- 四个 POST 都用 `try/except`：按钮 1 / 按钮 2 捕 `NumistaError`，按钮 3 / 按钮 4 捕 `NumistaPageError`；成功 → 302 回管理页（带原查询条件），结果区从**本地库**重读；失败 → **不重定向**，直接渲染 `coins/manage.html`（HTTP 200）并在表单上方显示错误文案。
- 国家候选取 `numista.ISSUER_CODES` 的 key（已收录国家）+ `list_coin_series_countries`；面值 / 材质候选取 `list_coin_series_denominations` / `list_coin_series_compositions`。三者都渲染成**可输入的 `<input list=…>` + `<datalist>`**（原生、无 JS、无 CDN）。
- 信息页 / 管理页结果区都按**面值 + 材质**做本地筛选（同一个 `search_coin_series`）。
- 管理页每行的**已采集状态**：`issue_stats` 给年份信息、`coin_series_value_counts` 给估值、`cached_image` 逐系列给图片（同 spec/coins.md §14.15：只认本地缓存），渲染成三个小标记（● 已采 / ○ 未采、`title` 有说明）。
- 逐年校验：年份非整数 / 面值为空 / 国家未选 → 400 或就地提示。

**模板与样式**

- 新建 `app/templates/coins/{info,series,manage}.html`；三页 `digest-head` 放同款子导航（拍卖 / 信息 / 采集管理，均为现有文字链接风格）。
- CSS 复用 `.sources-table` / `.table-wrap` / `.nowrap` / `.digest-head` / `.digest-meta` / `.empty` / `.btn` / `.search-form` / `.coin-facts` / `.row-actions`；`style.css` 末尾**新增少量类**：`.coin-series-card(-imgs/-img/-meta/-title/-line/-tag)`、`.coin-series-imgs-lg` / `.coin-series-fig` / `.coin-series-text` / `.coin-text-label`、`.form-error` / `.form-hint`，沿用既有 CSS 变量。
- 管理页结果行的「② / ③ / ④」三个按钮包在 `.row-actions` 里（既有类，小号 `.btn`）；**竖排**而非横排 —— 内容区 780px 下横排会把表撑到 ~900px、`.table-wrap` 横向滚动会**藏住按钮**；竖排后表刚好 738px 不滚动。另给「系列」列 `min-width: 220px`，避免操作列变宽后系列名被逐字竖排。
- 筛选行 `.search-row-labeled` 加 `flex-wrap: wrap` 并给控件 `min-width`：窄屏能自动换行，不挤成一坨。
- 接入：`coins/sessions.html` 的 `digest-meta` 追加 `· <a href="/coins/info">信息</a> · <a href="/coins/manage">采集管理</a>`；`base.html` 导航「铜币」仍指 `/coins`，不新增导航项。
- 详情页正反图加 `coin-zoom` 类 + `.coin-lightbox` 灯箱（样式 + `{% block scripts %}` 内联 JS，见 §18.15）。
- 详情页「各年份信息」13 列的**宽度**：默认 `.sources-table` 内边距 10/12px 下，13 列 min-content 累加 ≈ 773px > 内容区 738px → `.table-wrap` 内部横向滚动、最右「稀有度」被裁。故给该表加 `coin-series-table` 类并收窄到 `padding: 6px 8px`（省 ~104px），表宽回到 738px；备注列 `max-width: 200px; overflow-wrap: anywhere` 防长英文撑开。
- 管理页「已采集状态」列的三个标记见 `.coin-state(-on/-off)`（`::before` 出 ● / ○）。
- 管理页结果行**正 / 反图缩略图**：渲染在**「系列」列内、系列名上方**，复用已有的 `/coins/series/{id}/image/{side}` 路由与 `series_images` 上下文（`_render_manage` 已按 `cached_image` 给出 `{side: bool}`）；已缓存 → `<img class="coin-manage-img">`（56×56、`loading="lazy"`），未缓存 → `<span class="coin-manage-img coin-manage-img-empty">`（同尺寸虚线占位）。`.coin-manage-imgs` 出 flex 行（gap 6px）。**不单开「图片」列**：56×2 + 间距 + 内边距 ≈ 144px 会把表撑过内容区 738px。（同本节的宽表教训。）

**测试**（`tests/test_coins_pages.py` 增量，沿用「测试不发真实网络请求」）

| 用例 | 断言 |
|---|---|
| 查询页 | 空库 → 空态；有系列 → 渲染；国家 + 年份按区间过滤 |
| 详情页 | 字段齐全；**年份信息与估值合并为一张「各年份信息」表**（`各年份估值` 不再出现）；厂标 / 目录号 / 备注在表头；未知 id → 404 |
| 详情页（无单版 / 有单版无估值） | 无单版 → 「单版信息未采集」；有单版无估值 → 表照渲 + 「估值未采集」提示 |
| 年份统计 | 3 个版别落在 2 个年份 → `coin_series_issue_stats` 返回 (2, 3)，两页都显示「2 个年份 · 3 个版别」 |
| 管理页 | 表单 + 结果列表渲染；四个按钮都在；每行「已采集状态」显示年份 / 估值 / 图片三项 |
| 管理页状态 | 已采系列 → `年份 2 个 · 2 版` + `估值 1 条` + `图片 1/2`；未采系列 → 三项都显「未采」 |
| 管理页图片 | 结果行「系列」列渲染本地缓存的**正 / 反图缩略图**；未缓存 → 占位「未缓存」且不出现图路由、不外链 Numista |
| 估值条数聚合 | `coin_series_value_counts` 按 series 聚合（670 → 1 条；无估值的系列不在 key 里） |
| 按钮 1 | monkeypatch `numista.collect_series` → 断言以 (country, year, denomination) 调用 + 302 |
| 按钮 2 | monkeypatch `numista.collect_issues` → 断言以 series_id 调用 + 302 |
| 按钮 3 / 按钮 4 | monkeypatch `numista_web.collect_issue_values` / `download_series_images` → 断言以 series_id 调用 + 302 |
| 采集失败 | `collect_*` 抛 `NumistaError` / 按钮 3 抛 `NumistaPageError` → 页面 200 且含错误文案、不 500 |
| 图片 | 未缓存 → 路由 404 且页面给「原图链接」（信息页列表卡给占位块）；已缓存 → 路由返回字节且页面渲染 `<img>`；详情页含 `coin-zoom` 类与 `#coin-lightbox`（`IMAGE_DIR` 指到 tmp） |

---

## 18.15 Numista 图片本地缓存（对应 spec/coins.md §14.15）

**为什么**（实测事实）：图片 URL 在 `en.numista.com/catalogue/photos/...`，响应带 `cross-origin-resource-policy: same-origin` → 跨站 `<img>` 被浏览器以 `ERR_BLOCKED_BY_RESPONSE.NotSameOrigin` **必然拦截**；裸 `httpx`（带浏览器头）与 `curl` 又吃 Cloudflare 403。所以只能**下载到本地**再读，下载借 §18.13.2 的 CDP 链路（用户自己开的、已过 Cloudflare 的 Chrome）。

**模块**（`app/coins/numista_web.py` 增量，与 `collect_issue_values` 同文件、共用 `_cdp_hint`）

```python
IMAGE_DIR = PROJECT_ROOT / "data" / "coin_images"   # data/ 已在 .gitignore
IMAGE_SIDES = ("obverse", "reverse")

def image_path(numista_id: int, side: str, url: str | None = None) -> Path   # 保留原后缀，取不到用 .jpg
def cached_image(numista_id: int, side: str) -> Path | None                  # 页面据此判断
async def _fetch_bytes_via_cdp(cdp_url, type_id, urls, *, timeout_seconds) -> dict[str, bytes]
async def download_series_images(config, *, numista_id, cdp_url=None) -> dict[str, Path]
```

- `_fetch_bytes_via_cdp`：**先打开类型页过一遍 Cloudflare 校验**（拿到校验态），再**用页面导航**（`page.goto(图片 URL)` + `response.body()`）逐个取字节。实测 `context.request.get()`（Playwright 自己的请求）会 **403** —— 带不过浏览器的校验态，必须走真实导航。失败抛 `NumistaPageError`。
- 落盘前**先清同 side 的旧后缀文件**，避免 `.jpg`/`.png` 两份并存。

**CLI / 按钮**：`coins-images --series-id N [--cdp URL]` → 打印各 side 的文件名与字节数、目录；**不占 Numista 接口额度**。网页侧在管理页结果行加「**④ 下载图片**」（`POST /coins/manage/download-images`），同样同步 `await` `download_series_images`、失败捕 `NumistaPageError` 就地提示（与 CLI 同一函数）。

**路由**（`app/routers/coins.py` 增量）：`GET /coins/series/{numista_id}/image/{side}` → `side` 不在 `IMAGE_SIDES` 或没缓存 → 404；命中则 `FileResponse`。**不新增 StaticFiles 挂载、不改 main.py。**

**页面**：`coins/series.html` 的 `has_obverse_image` / `has_reverse_image` 由路由用 `cached_image()` 判定；缓存过才渲染 `<img src="/coins/series/{id}/image/{side}">`，否则渲染一行提示 + **原图链接**（外链，用户自己点），不出现破图。`coins/info.html` 的列表卡同理：路由给 `series_images`（逐系列判 `cached_image()`），缓存过才渲染本地图，未缓存的一侧给**占位块**（卡片整体是 `<a>`，嵌不了「原图链接」）。「各年份信息」表**去掉图片列**（`coin_issues.picture` 实测全 NULL）。

**点击放大**：详情页 `<img>` 加 `coin-zoom` 类；详情页末尾放一个 `<div class="coin-lightbox" id="coin-lightbox" hidden><img></div>` 灯箱，`{% block scripts %}` 里用**原生 JS**（无框架、无 CDN）绑定：点 `img.coin-zoom` → 把 `src` 赋给灯箱大图并取消 `hidden`；点遮罩 / 再点大图 / `Esc` → 关闭。CSS：`.coin-lightbox` 固定全屏 + `rgba(0,0,0,.72)` 遮罩 + `z-index:60`，`.coin-lightbox[hidden] { display:none }`（否则 `display:flex` 会盖过 `[hidden]` 的默认隐藏），大图 `max-width/height: 92vw/92vh`。

**表结构**：**不改**（靠文件是否存在判断），无 schema 变更、无迁移。

**测试**（`tests/test_coins_pages.py`，`IMAGE_DIR` → `tmp_path`、`_fetch_bytes_via_cdp` 换桩）：未缓存→404 + 页面出链接；已缓存→200 + 页面出 `<img>`；落盘写文件并清旧后缀；未知系列报错；详情页含 `coin-zoom` 类与 `#coin-lightbox`；管理页「④ 下载图片」桩掉 `download_series_images` 后 302。

---

## 18.16 历史购买品相录入（对应 spec/coins.md §14.16）

**定位**：给历史订单（`coin_orders`，§18.4）里的每个「国家 + 年份 + 面值」组合人工补一档品相。纯页面 + 人工录入，**不调接口、不进调度**。

**数据层（`schema_version` 6 → 7，纯新增表 → 登记进 `db.FORWARD_ADDITIVE_UPGRADES`，无需 ALTER）**

```sql
CREATE TABLE IF NOT EXISTS coin_purchase_grades (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  country      TEXT NOT NULL,
  era          TEXT NOT NULL,   -- 年份 / 年号（与 coin_orders.era 同源）
  denomination TEXT NOT NULL,
  grade        TEXT NOT NULL CHECK (grade IN ('好','普通','差')),
  updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_coin_purchase_grades
  ON coin_purchase_grades(country, era, denomination);
```

- **为什么不写 `coin_orders.condition`**：那是**订单级**、由 LLM 抽的**包浆描述**（如「巧克力包浆」），
  覆盖成 好/普通/差 会污染画像（`condition` 是画像维度之一）并影响每日推荐。本表**与画像 / 推荐无关**，只作人工参考。
- **一个组合一行**：去重键就是 `(country, era, denomination)`；重存为覆盖（upsert），选「未录入」则删行。

**统一（`app/coins/grades.py`，纯函数）**

历史订单里的 `era` / `denomination` 是 LLM 抄的**原样片段**，形态很杂（`1910` 与 `1910年`、
`1便士` 与 `一分` 并存）。统一成与 Numista 中文表一致的口径，**统一后的值既是展示值、也是去重键与落库键**：

- `normalize_country`：去殖民时期前缀（`英属新西兰` → `新西兰`、`荷属东印度` → `东印度`；
  真库里 182 个国家里有 36 个带 `英属 / 法属 / 荷属 / 葡属 / 德属` 前缀）；去掉后会变空则原样保留。
- `normalize_year`：去「年 / 年度」后缀只留纯数字（`1910年` → `1910`）；**年号纪年**取年号后的年份数字
  （中文数字转阿拉伯，`明治十五年` → `15`、`民国31年` → `31`、`康德元年` → `1`）；只有年号没有年份数字的
  （`光绪`）**原样保留**。
- `normalize_denomination`：前导数字（阿拉伯 / 中文含大写 `壹贰伍拾` / 分数 `1/4`、`¼`、`2½`）→ 阿拉伯数字，
  与单位之间加一个空格：`1便士`→`1 便士`、`一分`→`1 分`、`二十文`→`20 文`、`壹圆`→`1 圆`、
  `1/4安娜`→`1/4 安娜`、`¼安娜`→`四分之一 安娜`、`半便士`→`半 便士`；
  **认不出前导数字的原样保留**（`当制钱十文` / `大便士` / `克朗`）。
- `build_combos(orders, grades)`：按统一后的三元组去重计数（**只取三者齐全**的行），
  LEFT JOIN 已录品相；**已录品相但没有对应订单的组合（手工新增的）也作为一行**（`order_count = 0`）。
- `filter_combos(combos, country, era, denomination, grade)`：国家 / 年份 / 面值的查询词也先按同一口径统一，再用
  `app.textmatch.contains` 做宽松包含（输入 `1910年` 命中 `1910`、`1便士` 命中 `1 便士`）；
  `grade` 空 = 不限，其余按 好 / 普通 / 差 精确相等。
- `countries_of` / `denominations_of`：页面 `<datalist>` 候选（都是统一后的写法，如 `新西兰` / `1 便士`）。
- `order_ids_for_combo(rows, country, era, denomination)`：从 `repository.list_coin_order_keys()` 里挑出属于
  该组合（**统一后**）的订单 id —— 库里存的是**原始**写法（`1910年` / `一分`），删整行必须逐个统一后再比对，
  不能按列相等去删。

**repository 增量**（`app/repository.py`；SQL 只在此处，去重 / 统一 / 合并**不在**这里）

```python
list_coin_orders()                                                 # 已有：订单级原料，供 build_combos
list_coin_order_keys() -> list[tuple[int, str|None, str|None, str|None]]   # (id, country, era, denomination)
delete_coin_orders(ids: Sequence[int]) -> int                      # 按 id 删历史订单（删整行用）
list_coin_purchase_grades() -> list[CoinPurchaseGrade]             # 供测试 / 核对
upsert_coin_purchase_grade(country, era, denomination, grade)      # 按三键幂等写
delete_coin_purchase_grade(country, era, denomination) -> bool     # 选「未录入」/ 删整行时清除
```

**路由**（追加到 `app/routers/coins.py`）

| 方法 | 路径 | 职责 | 模板 |
|---|---|---|---|
| GET | `/coins/grades` | `build_combos` + `filter_combos`（国家 / 年份 / 面值 / 品相，均可空）→ 组合表；每行补派生列「所属收藏系列」（按国家 + 面值匹配收藏系列，不落库） | `coins/grades.html` |
| POST | `/coins/grades/add` | 手工新增一条组合（四项必填；统一后 upsert） | 302 → `/coins/grades` |
| POST | `/coins/grades/delete` | 删整行：`order_ids_for_combo` 反查订单 → `delete_coin_orders` + `delete_coin_purchase_grade` | 302 → 回查询页（带原筛选条件） |
| POST | `/coins/grades/save` | 保存整表：逐行 upsert / 删；键不完整或品相非法 → 400（整批不落库） | 302 → 回查询页（带原筛选条件） |

- **保存用「按行对齐的重复字段」**：每行 `<input type="hidden" name="country/era/denomination">` + `<select name="grade">`，
  四组字段按行序一一对应（浏览器原生表单即可，无需 JS）；另带 `filter_*` 三个隐藏字段，保存后重定向回同样的筛选结果。
  提交值落库前**再过一遍统一**（幂等，防止手改表单）。
- **品相取值**：`models.COIN_PURCHASE_GRADES = ("好","普通","差")`；下拉另有空值「未录入」（= 清除）。
- **删整行不能是嵌套 form**（行在「保存」大表单里，HTML 不允许嵌套表单）：故删按钮是 `type="button"`，
  页内二次确认后由**原生 JS 动态建一个隐藏表单**提交 `/coins/grades/delete`（连带把 `filter_*` 一起带上）。
  第一次点变「确认删除」并在一旁给「取消」，再点才真提交；确认态按钮加 `.btn-danger` 描红。
- **删整行的边界**：`country / era / denomination` 缺一的行**不显示**、因此也删不到；
  重跑 `coins-parse-orders` 会从 `orders.xlsx` 重新导入（删除不是永久屏蔽）；画像标签也不会自动重算。

**模板与样式**

- 新建 `app/templates/coins/grades.html`：查询表单 + **新增组合表单**（国家 / 年份 / 面值 / 品相四项必填）+ 结果表（含**操作列：删除**）+
  底部「保存」；删按钮的二次确认写在 `{% block scripts %}` 的原生 JS 里；`digest-meta` 放同款子导航，
  并在 `sessions.html` / `info.html` / `manage.html` 的子导航里**补「品相录入」入口**。
- `style.css` 末尾新增 `.coin-grade-table select`（行内下拉观感）、`.coin-grade-actions`（底部保存按钮，`sticky bottom` 吸底）、
  `.coin-filter-sticky`（查询表单 `sticky top` 吸顶）、`.coin-grade-collection`（派生列弱化显示）、
  `.coin-grade-del`（小号按钮）与 `.btn-danger`（二次确认态描红）；其余复用既有类。

**测试**（`tests/test_coins_pages.py` 增量，沿用「测试不发真实网络请求」）

| 用例 | 断言 |
|---|---|
| 统一（国家 / 年份 / 面值） | `英属新西兰`→`新西兰`、`荷属东印度`→`东印度`、`1910年`→`1910`、`1便士`→`1 便士`、`一分`→`1 分`、`二十文`→`20 文`、`壹圆`→`1 圆`、`1/4安娜`→`1/4 安娜`、`¼安娜`→`四分之一 安娜`、`半便士`→`半 便士`；年号纪年取数字（`明治十五年`→`15`、`康德四年`→`4`、`康德元年`→`1`），`光绪` / `当制钱十文` / `大便士` / `英属`（会变空）原样；已统一值幂等 |
| 去重 | 同值不同写法（`1808` / `1808年`、`一分` / `1分`、`英属新西兰` / `新西兰`）合并为一行；缺任一维度的订单**不出现** |
| 手工新增的组合 | 无订单也进列表，`order_count = 0` |
| 筛选 | 国家 / 年份 / 面值 / 品相分别命中；输入 `1808年` 命中 `1808` |
| 页面 | 组合数 = 结果表行数（隐藏键）；下拉含「未录入 / 好 / 普通 / 差」；查询表单含品相筛选；新增表单在页内；每行一个删按钮；删路径与「确认删除」在页内；展示统一后的值 |
| 派生列 | 「所属收藏系列」按国家 + 面值匹配收藏系列；匹配不上显示「—」 |
| 保存 | 302 回原筛选；统一后落库；重存不新增行；已录值回显 `selected` |
| 清除 | 选「未录入」→ 删该组合、其余保留 |
| 校验 | 品相非法 / 国家或年份为空 → 400，且**一行都不落库** |
| 新增 | 302；年份面值统一后落库；组合出现在列表 |
| 删整行 | 传**原始写法**也能删（按统一后组合比对）：该组合全部订单（含不同写法）+ 品相记录都删掉、其余订单不动；手工组合（无订单）删后只少品相行；键不完整 → 400 |

---

## 18.17 收藏画像与三层推荐（对应 spec/coins.md §14.17）

**数据层（`schema_version` 8 → 9，纯新增表 → 登记进 `db.FORWARD_ADDITIVE_UPGRADES`）**

```sql
CREATE TABLE IF NOT EXISTS coin_collection_series (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  collection_key TEXT NOT NULL UNIQUE,      -- 自定义 collection_series_id
  name           TEXT NOT NULL,             -- 国家 · 执政者 · 面值
  country        TEXT NOT NULL,
  ruler          TEXT,
  denomination   TEXT NOT NULL,
  size_mm        REAL,                      -- 代表尺寸（成员中位数）
  size_band      TEXT,                      -- 小 / 中 / 大 / 超大
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coin_collection_series_members (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  collection_id       INTEGER NOT NULL REFERENCES coin_collection_series(id) ON DELETE CASCADE,
  numista_series_id   INTEGER NOT NULL UNIQUE,
  numista_series_name TEXT,
  created_at          TEXT NOT NULL
);
```

**模块分工**

| 文件 | 职责 |
|---|---|
| `app/coins/collection.py` | 纯函数 + 组装：尺寸分档、Bronze/Copper 过滤、精制过滤、年份规格聚合、已有/缺失/升级年份 |
| `app/coins/explore.py` | 探索候选池：种子提取、Numista 目录扫描、材质 / 尺寸过滤、候选 CRUD 适配、采纳落库、P3 画像 |
| `app/coins/recommend.py` | 三层推荐排序、日本年号翻译、面值写法变体、当日拍品匹配、`rematch_all` |
| `app/repository.py` | 收藏系列 / 成员 CRUD、候选池 CRUD、`list_coin_today_lots`（按 `coin_sessions.auction_at` 取当日） |
| `app/routers/coins.py` | `/coins/collection`、`/coins/collections`、`/coins/explore` 及映射 / 候选状态 POST |
| `app/config.py` | `coins.collection`：`min_results` / `rarity_floor` / `top_upgrades_per_series` / `denomination_tiers` / `explore_*` |

**画像算法**

- 成员 series 先过滤材质；单版按 `year` 聚合，剔除 `is_proof` 或单版备注含 Proof/精制的条目；
  同一年按 `(有发行量, mintage, numista_id)` 降序取第一条。
- `rarity = frequency / 100`；`price_estimates = {VF: value_vf, XF: value_xf}`。
- 已有年份：`coin_orders` + `coin_purchase_grades` 按统一后的「国家 + 年份 + 面值」宽松匹配；
  需升级年份：已有年份中 `grade == "差"`。
- 收藏系列尺寸：显式 `size_mm` 优先，否则取成员 series 尺寸中位数。

**推荐算法**

- P1：`position_weight × scarcity_weight`；`position_weight` 首/末/执政者边界 1.5；
  `scarcity_weight = 1 / max(rarity, rarity_floor)`，无 rarity 时 1.0。
- P2：`XF/VF × 1/max(rarity, floor)`，每系列取前 `top_upgrades_per_series`。
- P3：仅当前两层与当日拍品的**匹配条数** < `min_results`（没有当日拍品时按推荐项数兜底）；
  候选来自 `load_explore_profiles()` → `explore.load_candidate_profiles()`（候选池状态为候选 / 采纳，忽略除外），
  取候选的缺失年份；按尺寸差升序、`-rarity` 升序。
- 匹配：拍品标题需同时命中 `country`、任一年份写法、任一面值写法；日本年号由
  `ruler` / 系列名 / `gregorian_year` 定位，输出 `明治6年` / `明治六年`。

**采集改动**

- `SessionCard` 增加 `end_at`（UTC ISO，来自列表页 `end_time`）；`list_sessions` 传入业务时区。
- `pipeline._collect` 先用 `repository.local_day_bounds` 过滤，只对当日截拍专场拉详情。
- `pipeline.run` 优先调用 `recommend.rematch_all`；收藏画像为空时回退 `matcher.rematch_all`。
- `_price_recommended` 新增 `keywords` 参数：新推荐直接使用推荐项关键词；旧画像仍从标题反推。

**页面**

- `/coins/collection`：**今日推荐**——4 个 KPI（精准匹配店铺 / 精准匹配拍品只统计 P1+P2）+ 两栏布局：左侧分段
  【精准匹配】（P1+P2，橙色高亮）与【相似推荐】（P3，灰色弱化；先与当日拍品匹配，只保留有命中的目标，
  最多 5 项，一个年份一张，标题按「国家 · 执政者 · 面值」，含推荐原因 / 发行量 / 稀有度 /
  尺寸重量 / VF-XF / 命中店铺与拍品）；
  可按系列 / 推荐原因 / 是否命中筛选（推荐原因只控制精准匹配；已命中过滤精准匹配的当日命中，未命中隐藏精准匹配）；
  右侧今日拍场为聚合面板，按「精准匹配 > 相似推荐 > 无关」排序：标题下显示精准 / 相似各自的推荐项数
  与当日命中件数；每行右侧居中圆圈显示该场总匹配件数，统计顺序为总件数 → 精准 → 相似；最多 6 场，
  其余折叠为「其他专场」（场次 · 命中件数）+ 查看（跳当日全部拍场）；卡片内直接列出店铺、拍品标题、当前价与专场链接；
  收藏画像摘要折叠在页尾。颜色沿用站点 `--accent` 蓝色主色，精准匹配用橙色强调、相似推荐用灰色弱化。
- `/coins/collections`：收藏系列——新建收藏系列表单 + 每个系列的成员表 / 加入 series / 删除系列；
  series 候选只列 Bronze/Copper，按国家 + 执政者 + 面值筛选（默认取收藏系列自身的值）。
- `/coins/explore`：候选池列表（Numista ID / 正反图 / 国家 / 执政者 / 面值 / 年份 / 规格 / 核实时间 / 状态），
  可按国家 + 执政者 + 面值逐级筛选（下拉只列当前范围内的值）；正反图点击用灯箱放大；
  每行状态下拉「候选 / 采纳 / 忽略」+ 保存；采纳写 `coin_series[_cn]` 与年份，忽略后列表隐藏。
- 铜币模块统一使用 `coins/_macros.html`：5 个一级入口（今日推荐 / 收藏 / 币库 / 候选池 / 拍场），
  收藏与币库各有子导航；旧版标签画像页面已移除。

**测试（`tests/test_coins_collection.py`）**

| 用例 | 断言 |
|---|---|
| 尺寸分档 | 24.9→小、25→中、30→大、35→超大 |
| 材质 / 精制过滤 | Bronze/Copper/青铜/铜通过；Copper-nickel/银剔除；`is_proof` 或单版备注 Proof 剔除 |
| 年份规格 | 同年多版取最大发行量；Frequency 0.6% → rarity 0.006；精制版不参与 |
| 画像 | 已有/缺失/升级年份正确；银币与精制年份不进入年份列表 |
| P1 | 首/末年位置权重 1.5；稀缺权重 1/rarity |
| P2 | 只取 `差`；每系列最多 3 个 |
| 年号 | `明治6年` / `明治六年` / `1873`；标题 `明治六年二钱` 能命中 |
| P3 | 前两层不足时用候选池画像补齐 |
| 候选池 | 忽略后列表隐藏且重扫不复活；材质 / 尺寸容差过滤；采纳后信息页与采集管理页可见 |
| 映射表 | 收藏系列 CRUD、成员增删 |
| 页面 | 新建 → 映射 → 两个页面 200 且渲染名称/年份；推荐表按系列 / 推荐原因 / 是否命中筛选；候选默认取系列执政者、可切「全部」；空库空态 200 |

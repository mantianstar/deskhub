# AI 个人工作台 — 技术方案（plan.md）

> 依据：[spec.md](./spec.md) v1。本文件只描述 **怎么实现**，不新增 spec 中不存在的能力。
> 凡是本文件与 spec 冲突的地方，在 §12 集中列出并说明理由。

---

## 1. 目标回顾与实现约束

| 项 | 结论 | 对实现的影响 |
|---|---|---|
| 使用者 | 仅本人 | 不做认证/权限；服务只绑 `127.0.0.1` |
| 形态 | 本地 Web 应用 | 单进程 Uvicorn + 内嵌调度器，不用 Docker/nginx |
| 成功标准 | 连续多天主动打开 | 开发顺序必须按 spec §13：先内容质量，后外壳 |
| 首要风险 | 内容不够好 | 打分理由（reason）是核心产物，要可校准、可反驳 |

**实现铁律**

1. 只走 RSS/Atom/公开 JSON，不做浏览器渲染，不做反爬对抗。
2. 单源失败绝不影响整体；任何外部调用（HTTP/LLM）都有超时与兜底。
3. 所有抓取/打分都是幂等的：重复执行不产生重复数据。
4. 不用 `try/except` 吞掉未知异常——失败要落库/落日志，因为「源挂了」本身就是产品信息。

---

## 2. 技术选型与关键决策

| 层 | 选型 | 决策理由 | 被否决的方案 |
|---|---|---|---|
| 语言 | Python 3.11+ | spec 指定；`zoneinfo`/`tomllib`/`asyncio.TaskGroup` 可用 | — |
| Web | FastAPI + Uvicorn | spec 指定，单 worker 即可 | Flask（async 抓取不便） |
| 存储 | SQLite（`sqlite3` 标准库）| 单用户，无运维；不引 ORM，SQL 直接可见可调 | SQLAlchemy（多一层抽象，单用户无收益） |
| 抓取 | `httpx`(async) + `feedparser` | spec 指定；`feedparser` 容错强，能吞脏 XML | `requests`（阻塞事件循环） |
| 调度 | APScheduler `AsyncIOScheduler` | 与服务同一事件循环，不额外占线程 | cron/systemd timer（脱离服务，看不到状态） |
| LLM | OpenAI 兼容 HTTP 接口（`httpx` 直连） | 不引 SDK，避免厂商 SDK 与 base_url 兼容坑 | `openai` SDK（一个厂商一个坑） |
| 前端 | Jinja2 + 少量原生 JS | spec 指定，无构建链 | React/Vite（维护成本 > 收益） |
| 配置 | YAML + `.env` + 环境变量覆盖 | 密钥不进仓库；源清单可手工编辑 | pydantic-settings（可用，但非必需） |
| 日志 | 标准库 `logging` + `RotatingFileHandler` | 无额外依赖 | loguru |

**依赖清单（`requirements.txt`）**

```
fastapi
uvicorn[standard]
jinja2
httpx
feedparser
apscheduler
pyyaml
python-dotenv
# dev
pytest
pytest-asyncio
respx
```

---

## 3. 系统架构

### 3.1 组件与数据流

```
                    ┌──────────────── APScheduler (AsyncIOScheduler) ─────────────────┐
                    │  job: startup_fetch (服务启动后立即)                            │
                    │  job: daily_pipeline (每天 08:00 Asia/Shanghai)                  │
                    └───────────────────────────┬─────────────────────────────────────┘
                                                │
                    ┌───────────────────────────▼─────────────────────────────────────┐
                    │ Pipeline (app/pipeline.py)                                       │
                    │  1. fetcher.run_once()      抓 RSS → 解析 → 去重 → items/fetch_runs│
                    │  2. scorer.run_pending()    items LEFT JOIN item_scores → item_scores│
                    │  3. 完成（日报为查询时实时计算，不落库）                          │
                    └───────┬───────────────────────────────┬───────────────────────────┘
                            │ httpx                         │ httpx
                    ┌───────▼────────┐              ┌───────▼─────────┐
                    │  RSS/Atom 源   │              │  LLM API        │
                    │  (外部)        │              │  (OpenAI 兼容)  │
                    └────────────────┘              └─────────────────┘

   浏览器  ──►  FastAPI routers  ──►  repository (纯 SQL)  ──►  data/deskhub.db
     │              digest / search / sources / placeholders / go
     └── Jinja2 templates（服务端渲染，无前端状态）
```

### 3.2 分层约定

| 层 | 文件 | 规则 |
|---|---|---|
| 入口 | `app/main.py` | 只做 app 装配、lifespan、路由注册、异常处理器 |
| 路由 | `app/routers/*.py` | 只做参数校验 + 调 repository + 渲染模板；不写 SQL、不发网络请求 |
| 业务 | `app/fetcher.py`、`app/scorer.py`、`app/pipeline.py` | 不感知 HTTP 请求上下文；可被 CLI 与调度器复用 |
| 数据 | `app/models.py`（DDL）、`app/repository.py`（读写函数） | 唯一允许出现 SQL 的地方 |
| 配置 | `app/config.py` | 全局唯一配置入口，启动时一次性加载并校验，失败即退出 |

**关键点**：`fetcher`/`scorer` 不依赖 FastAPI、不依赖 request 对象，因此 `python -m app.cli fetch` 能跑通同一条管道——这是调试与测试的前提。

---

## 4. 目录结构（落地版）

```
deskhub/
├── app/
│   ├── main.py              # FastAPI 入口、lifespan、路由注册
│   ├── config.py            # 配置加载与校验（含源清单、模型配置、模块关注点）
│   ├── db.py                # 连接工厂、PRAGMA、建表、schema 版本
│   ├── models.py            # DDL 常量 + dataclass（Source/Item/ItemScore/FetchRun）
│   ├── repository.py        # 所有 SQL 读写函数（纯函数，入参出参为 dataclass/dict）
│   ├── fetcher.py           # RSS 抓取、解析、URL 规范化、入库、fetch_runs
│   ├── scorer.py            # LLM 打分：prompt 组装、调用、JSON 校验、落库
│   ├── llm.py               # LLMClient 协议 + OpenAI 兼容实现 + FakeClient（测试用）
│   ├── pipeline.py          # fetch → score 编排（调度器与 CLI 共用）
│   ├── scheduler.py         # APScheduler 任务定义
│   ├── cli.py               # 手工命令：fetch / score / pipeline / purge
│   ├── logging_setup.py     # 日志格式与轮转
│   ├── routers/
│   │   ├── digest.py        # 首页日报、入口视图、/go/{id} 点击记录
│   │   ├── search.py        # 历史搜索
│   │   ├── sources.py       # 源管理（查看状态、启停、单源重试）
│   │   └── placeholders.py  # 基金 / 铜币 占位页
│   ├── templates/
│   │   ├── base.html
│   │   ├── digest.html
│   │   ├── module.html
│   │   ├── search.html
│   │   ├── sources.html
│   │   ├── placeholder.html
│   │   └── partials/item_card.html
│   └── static/style.css
├── config/
│   ├── config.yaml          # 应用 + LLM + 模块关注点
│   └── sources.yaml         # 源清单
├── data/
│   ├── deskhub.db           # 运行时生成，加入 .gitignore
│   └── logs/deskhub.log
├── tests/
│   ├── fixtures/*.xml       # 真实 RSS 样本（抓一次存下来）
│   ├── test_url_hash.py
│   ├── test_fetcher.py      # respx 模拟 HTTP
│   ├── test_scorer.py       # FakeClient，校验 JSON 解析与兜底
│   └── test_repository.py   # 用临时 db 文件跑 SQL
├── .env.example
├── .gitignore
├── requirements.txt
├── spec.md
├── plan.md
└── README.md                # 启动方式与调试命令（M0 产出）
```

相对 spec §4 的差异（3 处新增 + 1 处拆分），理由见 §12。

---

## 5. 配置设计

### 5.1 `config/config.yaml`

```yaml
app:
  host: 127.0.0.1
  port: 8765
  timezone: Asia/Shanghai
  db_path: data/deskhub.db
  log_path: data/logs/deskhub.log

digest:
  limit: 8              # 日报条数（spec: 5-10）
  lookback_hours: 48    # 候选池回溯窗口（见 §7）

fetch:
  timeout_seconds: 10
  user_agent: "deskhub/1.0 (personal rss reader)"
  max_items_per_source: 30    # 首次抓取历史条目上限，防首跑灌爆
  max_concurrency: 1          # spec §6.2 要求串行；>15 个源时再调

scoring:
  concurrency: 3
  max_summary_chars: 1500     # 送模型的摘要截断
  max_attempts: 2             # 单条最多重试次数
  prompt_version: v1
  temperature: 0.2

llm:
  base_url: https://api.example.com/v1
  model: deepseek-v4-flash
  api_key_env: DESKHUB_LLM_API_KEY   # 只写环境变量名，不写密钥本身

modules:
  agent:
    label: Agent 开发
    focus: >
      框架与工具动态（LangGraph/AutoGen/MCP/agent SDK）、论文与方法（规划、
      记忆、多智能体协作、评测）、实战案例与踩坑、Agent 产品与行业动态。
  bigdata:
    label: 大数据
    focus: >
      Hadoop/Spark/Flink/湖仓（Iceberg/Hudi/Paimon）/Hive 技术栈；
      数据工程与实时计算、数据治理、调度编排（Airflow/DolphinScheduler）、
      性能调优与生产事故复盘。
```

### 5.2 `config/sources.yaml`

```yaml
sources:
  - name: InfoQ 中文
    url: https://www.infoq.cn/feed
    module: agent
    enabled: true
    published_tz: Asia/Shanghai   # 可选，见下
  # ... 其余见 §15 首批源清单
```

`url` 唯一：重复的 url 在启动校验时直接报错退出（防止同一源配两遍导致重复抓取）。

`published_tz`（可选）：仅当「该源的 pubDate 写的是**本地时间**却标成 GMT/UTC」时才填，抓取时按这个时区把时间重新解释成 UTC。目前只有 InfoQ 中文需要（实测依据见 §12 第 12 条）。不填的源维持原行为：feed 标什么时区就按什么时区解析。

**源清单是唯一真源**：`sync_sources_from_config()` 启动时按 url upsert，配置里已不存在的源自动置 `enabled = 0`（**不删行**，保留 `items` / `fetch_runs` 历史）。故「淘汰一个源」= 从 `sources.yaml` 里删掉它，不要手工改库。

### 5.3 密钥与覆盖规则

- 优先级：**环境变量 > `.env` > `config.yaml`**。
- 覆盖键：`DESKHUB_LLM_API_KEY`、`DESKHUB_LLM_BASE_URL`、`DESKHUB_LLM_MODEL`、`DESKHUB_DB_PATH`。
- `.env` 进 `.gitignore`，仓库只提交 `.env.example`。
- 启动校验：`api_key` 为空且 `scoring` 已启用时，**打印警告但仍允许启动**（否则无法调试抓取）。

---

## 6. 数据层设计

### 6.1 时序与类型约定

- 所有时间列存 **UTC ISO8601 字符串**（`2026-09-21T03:12:00Z`），SQLite 无原生时间类型，字符串可排序、可比较。
- 「今天」的业务边界按 `config.app.timezone` 计算，换算成 UTC 后参与查询（避免跨时区导致的日报错日）。
- 时间读写统一走 `repository._utcnow()` / `parse_dt()`，禁止各模块自己 `datetime.now()`。

### 6.2 DDL（`app/models.py`）

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS sources (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL,
  url         TEXT    NOT NULL UNIQUE,          -- feed 地址
  module      TEXT    NOT NULL CHECK (module IN ('agent','bigdata')),
  enabled     INTEGER NOT NULL DEFAULT 1,
  last_ok_at  TEXT,
  fail_count  INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  module       TEXT    NOT NULL,                -- 冗余自源配置，便于跨 module 查询不 JOIN
  title        TEXT    NOT NULL,
  url          TEXT    NOT NULL,                -- 原始链接（用于跳转展示）
  url_hash     TEXT    NOT NULL UNIQUE,         -- 规范化后 sha256，去重键
  published_at TEXT,
  summary      TEXT,
  fetched_at   TEXT    NOT NULL,
  clicked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_fetched  ON items(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_module   ON items(module, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_clicked  ON items(clicked_at) WHERE clicked_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS item_scores (
  item_id        INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  score          REAL    NOT NULL CHECK (score >= 0 AND score <= 100),
  reason         TEXT    NOT NULL,
  model          TEXT,
  prompt_version TEXT    NOT NULL,
  scored_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_score ON item_scores(score DESC);

CREATE TABLE IF NOT EXISTS fetch_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  started_at  TEXT    NOT NULL,
  finished_at TEXT,
  status      TEXT    NOT NULL,   -- ok | http_error | parse_error | timeout | empty
  http_status INTEGER,
  items_new   INTEGER DEFAULT 0,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_source_time ON fetch_runs(source_id, started_at DESC);

CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

**迁移策略**：`schema_meta.schema_version` 从 `1` 起。v1 只做「建表 if not exists + 版本号校验」，不做自动 ALTER；未来加列时写 `app/migrations/vN.py` 手工执行。

### 6.3 连接策略

- 单用户、单进程，但调度器任务与其他请求共享事件循环，故采用 **按操作取连接**（`db.connect()` 上下文管理器），不维护全局长连接。
- 写操作放在 `asyncio.to_thread` 中执行，避免 `sqlite3` 阻塞事件循环；`WAL + busy_timeout` 兜住并发写。
- 所有写操作包在单个事务里（`with conn:`），保证 `items` 插入与 `fetch_runs` 写入的原子性。

### 6.4 repository 接口清单

| 函数 | 用途 |
|---|---|
| `sync_sources_from_config(sources)` | 启动时把 YAML 源清单 upsert 进 `sources`（按 url 匹配，保留运行时状态字段）；配置里已不存在的源自动置 `enabled = 0`（不删行，保留历史 `fetch_runs`） |
| `list_sources(only_enabled=False)` | 源列表（含 `fail_count`、`last_ok_at`） |
| `set_source_enabled(source_id, enabled)` | 启停开关 |
| `insert_item_if_new(item) -> int \| None` | 靠 `url_hash` UNIQUE 冲突返回 None 实现幂等 |
| `record_fetch_run(...)` / `mark_source_ok(id)` / `mark_source_fail(id)` | 抓取日志与源状态转移 |
| `list_pending_score_items(limit, lookback_days)` | `items LEFT JOIN item_scores WHERE item_id IS NULL` |
| `upsert_score(item_id, score, reason, model, prompt_version)` | 打分落库 |
| `query_digest(day_start_utc, day_end_utc, module, limit)` | 日报查询（§7.1） |
| `search_items(q, module, start, end, page, page_size)` | 历史搜索（标题 + 摘要 LIKE，分页） |
| `mark_clicked(item_id)` | 首次点击才写 `clicked_at`（`COALESCE`，保留首次时间） |
| `purge_before(dt) -> dict` | 手动清理（删除 items 级联删除 scores） |

---

## 7. 抓取管道（`app/fetcher.py`）

### 7.1 单源流程

```
1. 取 enabled=1 的源（按 module 排序，串行处理）
2. 取当前时间 now，写入 fetch_runs(started_at, status='running' 占位不落库，仅内存)
3. httpx.AsyncClient(timeout=10, follow_redirects=True, headers=UA)
   GET feed url  → 记录 http_status
4. feedparser.parse(resp.content)
   - bozo 且 entries 为空 → status='parse_error'
   - entries 为空但解析正常 → status='empty'（不增加 fail_count，可能是源暂时无更新）
5. 按 published/updated 降序取前 max_items_per_source 条
6. 逐条：
   a. url = entry.link（缺失则跳过）
   b. url_hash = sha256(canonicalize(url))
   c. published_at = 解析 entry.published_parsed / updated_parsed，失败则 None
      （源声明了 `published_tz` 时按该时区重新解释，见 §12 第 12 条）
   d. summary = 优先 entry.summary，回退 entry.description，HTML 去标签后截断 2000 字
   e. insert_item_if_new → 计数 items_new
7. 更新源状态 + 写 fetch_runs
```

### 7.2 URL 规范化（去重键核心）

`canonicalize(url)` 规则，逐条命中即改：

1. scheme 统一 `https`
2. host 小写、去 `www.` 前缀
3. 去掉 fragment（`#...`）
4. 去掉跟踪参数：`utm_*`、`fbclid`、`gclid`、`ref`、`source`、`spm`、`from`
5. 路径去除末尾 `/`（根路径除外）、合并重复 `/`
6. 剩余 query 按 key 排序

> 该函数是唯一被单测覆盖到「边界穷举」的地方——去重错一条，日报就会出现重复内容。

### 7.3 失败分类与源状态机

| 情况 | status | fail_count | last_ok_at |
|---|---|---|---|
| HTTP 2xx 且解析出条目 | `ok` | 归零 | 更新 |
| HTTP 2xx 但 0 条 | `empty` | 不变 | 更新（源是活的） |
| HTTP >=400 | `http_error` | +1 | 不变 |
| 超时 / 连接失败 | `timeout` | +1 | 不变 |
| feedparser bozo 且无条目 | `parse_error` | +1 | 不变 |

**标红阈值**：`fail_count >= 3`。界面红点 + tooltip 显示最后一次 `error` 与 `http_status`。
**幂等**：源解析成功但条目全部已存在时，`items_new=0`，也算 `ok`（不误报故障）。

### 7.4 并发与超时

- v1 按 spec 串行。当源数 > 15 或单轮耗时 > 60s 时，把 `fetch.max_concurrency` 调到 3~5（代码里用 `asyncio.Semaphore`，默认 1），无需改结构。
- **单源整体超时**：`asyncio.wait_for(..., timeout=timeout+5)`，兜住 feedparser 之外的意外挂起。

---

## 8. LLM 打分（`app/scorer.py` + `app/llm.py`）

### 8.1 接口抽象

```python
class LLMClient(Protocol):
    async def complete_json(self, system: str, user: str) -> str: ...
    @property
    def usage(self) -> Usage: ...   # M2 实测补充：累计 token，供成本打印（§8.4）
    async def aclose(self) -> None: ...  # 一个 run 共用一个 httpx 连接池，结束时关闭
```

- `OpenAICompatClient`：`POST {base_url}/chat/completions`，`Authorization: Bearer`，尽量带 `response_format={"type":"json_object"}`；若厂商不支持（400）则降级为纯 prompt 约束并缓存「不支持」标记，后续请求不再带该字段。
- `FakeClient`：测试注入，返回固定 JSON 或故意返回坏 JSON。
- 通过 `llm.get_client(config)` 获取，测试用 `monkeypatch` 替换。

### 8.2 Prompt（prompt_version = v1）

**system**

```
你是信息筛选助手，只输出 JSON，不要任何解释文字。

请评估下面这条内容对「{module_label}」方向读者的价值。
该方向关注：{module_focus}

打分口径（总分 100）：
- 相关度 0-50：与上述关注点的直接相关程度。泛泛的行业融资新闻最多 15 分。
- 信息密度 0-30：是否给出具体方法、数据、架构、可复现结论。纯观点/营销稿最多 10 分。
- 时效性 0-20：越新越高。超过 30 天的内容最多 8 分。

不要因为来源知名而加分。宁可给低分，也不要为了填满而抬分。

reason 要求：20-60 字，必须具体——说清「这条讲了什么 + 为什么和读者相关」。
禁止出现「内容优质」「值得一读」「干货满满」这类空话，禁止复述标题。

输出格式：{"score": <0-100 的整数>, "reason": "<中文一句话>"}
```

**user**

```
标题：{title}
来源：{source_name}
发布时间：{published_at or 未知}
摘要：{summary_truncated or （无摘要，请仅依据标题判断，并相应降低信息密度分）}
```

### 8.3 输出校验与兜底

| 步骤 | 失败处理 |
|---|---|
| 去掉 ``` 代码围栏、取第一个 `{...}` | 仍无 JSON → 重试 1 次（附加「只输出 JSON」） |
| `json.loads` | 异常 → 同上 |
| `score` 转 float 并 clamp 到 [0,100]；非数字 → 视为失败 | 失败 |
| `reason` 去空白，长度 < 10 字 → 视为失败 | 失败 |
| 重试仍失败 | **不写 item_scores**，记 WARNING 日志；条目留在待打分池，下轮自然重试 |

### 8.4 调度与成本

- 待打分池：`items` 近 `lookback_days=7` 天、无 `item_scores` 记录、`limit` 由管道传入（默认 200）。
- 并发 3（`asyncio.Semaphore`），每条独立重试，互不影响。
- 按 spec §7「每条一次调用」，不做批量合并（批量会牺牲理由的针对性，而理由是本产品的核心资产）。
- 成本 = 入池条数 × (prompt ≈ 700 in + 80 out tokens)。日志中打印本轮 `items_scored` / `items_failed` / 累计 token，便于事后核对账单。
  **M2 实测值（2026-09-22，DeepSeek，112 条）**：prompt 47786（≈427/条）+ completion 39361（≈351/条）= 87147 tokens/轮，耗时 91s（并发 3，单条 ≈2.4s）。输出 token 是估算的 4 倍多，核对账单请用实测值。
- **重打分**：`python -m app.cli score --rescore --prompt-version v1` 会删除该版本打分并重跑；改 prompt 时 `prompt_version` 必须递增。

---

## 9. 日报、排序与反馈

### 9.1 候选池定义（必须先定死，否则「今日」会漂移）

```
候选池 = items 满足 day_start_utc <= fetched_at < day_end_utc
（day_start 按 Asia/Shanghai 当日 00:00 计算）
```

理由：日报要回答的是「**今天**新到的东西里哪些值得看」，用 `fetched_at` 而非 `published_at` 才不会因为源发布时间很久（如 arXiv 前置日期）而漏掉今日抓到的内容。
若当日候选数为 0（服务当天没跑过），回退到 `lookback_hours=48` 内，页面顶部标注「非今日数据」。

### 9.2 排序规则

```sql
ORDER BY (sc.score IS NULL),        -- 已打分在前
         sc.score DESC,             -- 分高在前
         i.published_at DESC,       -- 并列时新的在前
         i.id DESC
LIMIT :limit
```

- 未打分条目排在末尾（spec §7 要求），并在卡片上显示「未评分」而不是空白分数。
- v1 不引入 `clicked_at` 参与排序（样本量不足会过拟合）；点击数据只用于事后人工看 prompt 是否需要调。

### 9.3 隐式反馈

- 卡片外链指向 `/go/{item_id}`，该路由：`mark_clicked`（`COALESCE(clicked_at, now)` 保留首次）→ `302` 到 `items.url`。
- 不校验 url 是否仍在白名单（单用户本地，风险可接受），但要校验 `item_id` 存在，否则 404。
- 为区分「每天都点」与「从没点过」，sources 页与搜索页也走 `/go/{id}`。

---

## 10. 页面与路由

| 路由 | 方法 | 职责 | 模板 |
|---|---|---|---|
| `/` | GET | 今日日报（全模块混合，前 `digest.limit` 条） | `digest.html` |
| `/m/{module}` | GET | 入口视图：`agent` / `bigdata` 今日列表 | `module.html` |
| `/search` | GET | 关键词（LIKE 标题+摘要）+ 时间范围 + module + 分页 | `search.html` |
| `/sources` | GET | 源列表：module、启停、最近成功、失败次数（`>=3` 标红） | `sources.html` |
| `/sources/{id}/toggle` | POST | 启停（`enabled` 取反），302 回 `/sources` | — |
| `/sources/{id}/fetch` | POST | 立即抓单个源（调试/自愈用），302 回 `/sources` | — |
| `/go/{item_id}` | GET | 记 `clicked_at` + 302 外链 | — |
| `/funds` | GET | 基金监控占位（「未启用」） | `placeholder.html` |
| `/coins` | GET | 铜币拍卖占位（「未启用」） | `placeholder.html` |
| `/healthz` | GET | JSON：`{db, sources_total, sources_failing, last_fetch_at}` | — |

**模板约定**

- `base.html`：导航栏（日报 / Agent / 大数据 / 历史搜索 / 源管理 / 基金 / 铜币）+ 内容块 + 一行原生 JS。
- `partials/item_card.html`：`标题 / 来源 / 打分 / 理由 / 时间`，被 digest、module、search 三处复用（保证同一套视觉）。
- 原生 JS 只做两件事：搜索页的「展开高级筛选」、点击外链时置灰防重复点。无框架、无 CDN。
- 打分视觉分级：`>=75` 绿、`50-74` 灰、`<50` 浅色、无分「未评分」。颜色语义写在 CSS 变量里，便于后面调。

---

## 11. 调度、运行与运维

### 11.1 启动流程（`app/main.py` lifespan）

```
1. logging_setup()                      # 日志先就位
2. config.load()                        # 校验失败 → 打印错误并 exit(1)
3. db.init_db()                         # PRAGMA + 建表 + schema_version 校验
4. repository.sync_sources_from_config()# YAML 源清单 → sources 表
5. scheduler.start()                    # AsyncIOScheduler
6. asyncio.create_task(pipeline.run())  # 启动补拉（spec §9），不阻塞服务可用
7. yield（服务运行）
8. scheduler.shutdown()（关闭时）
```

第 6 步不 `await`：源多时首轮抓取可能几十秒，不能让首页等到超时。日报页在无数据时展示「首次抓取进行中」。

### 11.2 任务清单

| job | 触发 | 说明 |
|---|---|---|
| `startup_fetch` | 启动后立即（一次性） | 覆盖休眠/关机期间漏跑（spec §9） |
| `daily_pipeline` | 每天 `08:00` Asia/Shanghai | `coalesce=True`、`misfire_grace_time=3600`、`max_instances=1` |

- `misfire_grace_time=3600`：本机 08:05 才醒，任务仍然执行而不是被丢弃。
- `max_instances=1`：手工 CLI 与定时任务重叠时不并跑（`items` 的 UNIQUE 冲突只是兜底）。
- 抓取与打分之间加一个短间隔（`await asyncio.sleep(1)`）纯为日志可读，无功能意义。

### 11.3 CLI（`python -m app.cli <cmd>`）

| 命令 | 用途 | 对应里程碑 |
|---|---|---|
| `fetch [--source-id N] [--dry-run]` | 抓取（`--dry-run` 只打印解析结果不写库） | M1 调试 |
| `score [--limit N] [--rescore --prompt-version v1]` | 打分 | M2 校准 |
| `pipeline` | 抓取 + 打分 | 手工出报 |
| `sources` | 打印源状态表格 | M1 |
| `purge --before YYYY-MM-DD [--yes]` | 手工清理旧数据（spec §11 保留策略） | M6 |

---

## 12. 与 spec 的差异说明

| # | 差异 | 理由 |
|---|---|---|
| 1 | 新增 `app/repository.py` | spec §4 未列数据访问层；若不显式分层，SQL 会散落到 router 里，测试与 CLI 复用都困难 |
| 2 | 新增 `app/llm.py`、`app/pipeline.py` | 前者为了注入 FakeClient 做测试；后者让调度器与 CLI 共用同一编排逻辑 |
| 3 | 新增 `app/cli.py`、`app/logging_setup.py` | spec §11 明确要求「手动 purge 脚本」，且调试必须有手工触发入口 |
| 4 | 新增 `config/config.yaml`（spec 只列了 `sources.yaml`） | LLM 配置、模块关注点、日报条数都是配置项，写死在代码里会导致改 prompt 就要改代码 |
| 5 | `fetch_runs.status` 增加 `empty` 取值 | spec 只列 ok/http_error/parse_error/timeout。源解析成功但 0 条不应算失败，否则长期无更新的源会被误标红 |
| 6 | 新增 `schema_meta` 表 | SQLite 无版本机制，手工加列前需要知道当前版本 |
| 7 | 日报候选池用 `fetched_at` 而非 `published_at` | spec §2.4/§6.7 未明确；用 `published_at` 会漏掉「今天抓到的旧发布日期内容」 |
| 8 | 新增 `/go/{id}` 中转路由 | spec §8 要求「点击链接时记录 `clicked_at`」，必须经过服务端才能记录 |
| 9 | 新增 `/healthz`、`/sources/{id}/fetch` | 本地长期运行的自检最小集；`POST /fetch` 让挂掉的源可以单独重试 |
| 10 | 首批源只接国内可直连的中文源（spec §2.3 原本也允许「英文源（HN 等）」这一类） | 实测国外源在本机网络不可达；接不通的源会让日报全是空的，直接命中 spec §1 的首要风险。清单见 §15 |
| 11 | `sync_sources_from_config()` 会把配置里已不存在的源自动置 `enabled = 0` | spec §2.3 说「首批源写死在配置中」，但没规定配置删源后库里那行怎么办。默认 upsert 会让被删的源永远留在表里且继续被抓（M0 收尾时实际发生过：HN 占位已从 `sources.yaml` 删掉，库里仍 `enabled=1`） |
| 12 | 源配置新增可选字段 `published_tz`，抓取时按它把 feed 时间重新解释成 UTC | spec §2.3 只要求「只接入有 RSS 的源」，没规定「feed 的时间标注本身就错」时怎么办。实测 **InfoQ 中文的 feed 把北京时间当 GMT 标**（2026-09-23：它的 channel `pubDate` 自称 `10:25:06 GMT`，而那一刻真实 UTC 是 `02:25:20`，超前整整 8 小时；09-22 那批里它标称的最新一条比抓取时刻还晚 7 小时，物理上不可能）。照字面存会让该源 24 条 `published_at` 全部偏 8 小时：卡片显示成未来时间、prompt 的「时效性 20 分」虚高、并列排序跟着偏。备选方案「published_at 晚于 fetched_at 就存 NULL」只救得了其中 1 条（其余 23 条只是偏、没到未来），故选择源级声明；这是数据修正，不是 plan §16 禁止的「插件式数据源抽象」 |

除此之外，数据表字段与 spec §5 完全一致（仅补了必要索引与 `CHECK` 约束）。

---

## 13. 测试策略

### 13.1 必测项（无这些不上线）

| 用例 | 断言 |
|---|---|
| `canonicalize` 边界 | utm 参数/`www.`/尾斜杠/`#`/大小写 host 归一化后 hash 相同；不同文章 hash 不同 |
| 同一 feed 抓两次 | 第二次 `items_new=0`，`items` 总数不变 |
| 源返回 500 | `status='http_error'`、`fail_count=1`、`last_ok_at` 不变、其他源仍被处理 |
| 源返回合法 XML 但 0 条 | `status='empty'`、`fail_count` 不变 |
| 连续 3 次失败后成功 | `fail_count` 归零、界面不再标红 |
| LLM 返回坏 JSON | 重试 1 次；仍坏则**不写** `item_scores`，条目留在待打分池 |
| LLM 返回 `score=150` / `"95"` | clamp 到 100 / 转成 95 |
| 日报排序 | 有分在前且降序；未评分在末尾；并列按 `published_at` 降序 |
| 跨日边界 | 上海 00:00:00 与 23:59:59 抓到的条目分属不同日报 |
| `/go/{id}` 点击两次 | `clicked_at` 保持首次 |
| 配置里删掉一个源后再 sync | 该源 `enabled=0` 且行保留（不级联删 `items`/`fetch_runs`），其余源不受影响 |
| 重复 sync 同一清单 | `sources` 行数不变，`last_ok_at` / `fail_count` 不被配置覆盖 |
| 源声明 `published_tz` | 标成 GMT 的本地时间按声明时区折算（`09:26 GMT` → `01:26Z`）；未声明的源不受影响（plan §12 第 12 条） |

### 13.2 手工验收（对应 spec §10）

1. `cli fetch` 后 `sqlite3 data/deskhub.db "select count(*) from items"` > 0。
2. `cli score --limit 10` 后人工读 10 条 reason：**能逐条反驳**（不能反驳就是 prompt 不合格，回改 §8.2）。
3. 打开首页能看到 5-10 条，点击跳转，`clicked_at` 有值。
4. 搜索关键词能命中断言当日抓到的条目。
5. 手工把某源 url 改成不存在的域名 → 连跑 3 次 → 源管理页标红，其他源正常。
6. 连续多天主动打开（唯一真正重要的验收项）。

---

## 14. 开发里程碑（严格按 spec §13 顺序）

| 里程碑 | 范围 | 产出 | 验证方式 |
|---|---|---|---|
| **M0 骨架** | `requirements.txt`、`config.py`、`db.py`、`models.py`、`repository.py`、`main.py`、`logging_setup.py`、`.gitignore`、`README.md` | 服务能启动，`/healthz` 返回 JSON，`data/deskhub.db` 建表成功 | `uvicorn app.main:app` + curl `/healthz` |
| **M1 管道** | `fetcher.py`、`cli.py fetch/sources`、`sources.yaml`（5 个源） | 真实源入库；`fetch_runs` 有记录；源失败可分类 | §13.1 前 5 用例 + `cli sources` 看状态 |
| **M2 打分** | `llm.py`、`scorer.py`、`cli score` | 每条条目有 `score` + `reason`；理由可反驳 | 人工读 10 条理由；坏 JSON 用例通过 |
| **M3 日报** | `pipeline.py`、`routers/digest.py`（`/`）、`base.html`+`digest.html`+`item_card.html`、`/go/{id}` | 首页 5-10 条精选，点击被记录 | §13.2 第 3 条；跨日边界用例 |
| **M4 历史搜索** | `routers/search.py`、`search.html` | 关键词 + 时间 + module 筛选，分页 | §13.2 第 4 条 |
| **M5 外壳** | `routers/digest.py`（`/m/{module}`）、`sources.py`+`sources.html` 标红、`placeholders.py`、导航栏、`style.css` | 两入口视图 + 源管理 + 两个占位页 | §13.2 第 5 条；手工点完所有导航 |
| **M6 收口** | `scheduler.py`、`cli purge`、日志轮转、`tests/` 补齐 | 定时出报、手动清理、测试全绿 | `pytest` 通过；改系统时间或改 cron 验证 misfire |

**M1 的额外动作**：把每个源的原始 XML 存到 `tests/fixtures/`，后续所有解析改动都靠离线样本回归，不在测试里访问真实网络。

---

## 15. 首批源清单（待 M1 逐条验证）

国外源（HN、Google AI Blog、arXiv 等）在本地网络不可达，v1 一律用国内可直连的中文源。下表已实测 `http_status=200` 且有条目，入库前仍建议用 `cli fetch --dry-run` 复核；不可达的直接换掉，不要为了凑数保留坏源。

| module | 源 | URL | 实测（2026-09） |
|---|---|---|---|
| agent | InfoQ 中文 | `https://www.infoq.cn/feed` | 200，20 条 |
| agent | 量子位 | `https://www.qbitai.com/feed` | 200，10 条 |
| agent | 掘金 | `https://juejin.cn/rss` | 200，20 条 |
| bigdata | 开源中国资讯 | `https://www.oschina.net/news/rss` | 200，50 条 |
| bigdata | 美团技术团队 | `https://tech.meituan.com/feed/` | 200，10 条 |
| bigdata | 博客园首页 | `https://feed.cnblogs.com/blog/sitehome/rss` | 200，20 条 |

> 只接入有 RSS/Atom 的源（spec §2.3）。中文技术社区（掘金标签页等）若无官方 RSS，v1 一律不接——不为了「看起来全」引入反爬和不稳定。

---

## 16. 风险与对策（实现层）

| 风险 | 触发信号 | 对策 |
|---|---|---|
| 日报候选池条数不足（spec §11 目标 ≥30/天） | 连续 3 天候选 < 20 | 源数量不足是主因；按 §15 先把 bigdata 源补到 5 个以上，再考虑放宽 `lookback_hours` |
| LLM 与 OpenAI 协议不兼容 | 400/404 且非 token 问题 | `response_format` 自动降级；`base_url` 走配置可换厂商；保留原始响应 body 到日志 |
| LLM 调用成本超预期 | 日志中 token 累计 | 入池优先用「当日新增 + 未打分」，不重跑历史；摘要截断 1500 字 |
| 打分质量差（理由空泛） | 人工读 reasons 无法反驳 | 只改 §8.2 的 prompt 并递增 `prompt_version`，用 `cli score --rescore` 对比新旧理由，不靠感觉 |
| 源集体失效 | 源管理页大面积标红 | 单源失败已隔离；把源清单当配置维护，失效源改为 `enabled: false` 而非删除（保留历史 `fetch_runs`）。从 `sources.yaml` 删掉的源由 `sync_sources_from_config()` 在下次启动时自动停用，不需要手工改库 |
| SQLite 写冲突 | `database is locked` | WAL + `busy_timeout=5000` + 写操作集中在 `to_thread`；若仍出现，改为全局 `asyncio.Lock` 串行写 |
| 服务未启动导致漏天 | 日报页显示「非今日数据」 | 已由启动补拉兜底；接受漏天（spec §9 明确接受），不做开机自启 |

**明确的「不做」清单（防止实现时膨胀）**：不做用户体系、不做点赞/踩、不做 AI 自动发现源、不做浏览器渲染抓取、不做标题相似度去重、不做调仓/交易建议、不做插件式数据源抽象、不做自动数据清理。

---

## 17. 需要你拍板的 3 个点

1. **日报条数**：默认 8 条（区间 5-10）。若你希望固定 10 条，改 `digest.limit` 即可。
2. **打分并发与重试**：默认并发 3、失败重试 1 次。若你的厂商 API 有严格 RPM 限制，需下调并发。
3. **服务端口**：默认 `8765`（避开 8000/3000 常见占用）。若与本机其他服务冲突请指定。

其余参数均已按 spec §11 的你的设定落进 `config/config.yaml`（08:00 出报、全量保留 + 手动 purge、`deepseek-v4-flash`）。

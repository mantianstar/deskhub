# AI 个人工作台 — 技术方案：通用部分

> 本文件是 [plan.md](../plan.md) 的**通用部分**（选型 / 架构 / 目录 / 配置 / 数据层 / 调度运维 / 差异 / 测试 / 风险 / 运行控制）。
> 编号沿用 `§1`~`§6`、`§11`~`§13`、`§16`、`§17`、`§19`。
> **跨文件编号**：`§7`~`§10`、`§14`、`§15` → [knowledge.md](./knowledge.md)；`§18.x` → [coins.md](./coins.md)；`§14.x` → [../spec/coins.md](../spec/coins.md)。
> **变更过程与理由**见 [CHANGELOG.md](../CHANGELOG.md)。

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


## 3. 系统架构

### 3.1 组件与数据流

```
                    ┌──────────────── APScheduler (AsyncIOScheduler) ─────────────────┐
                    │  job: daily_pipeline (每天 08:00 Asia/Shanghai，按开关执行)       │
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
     │              digest / search / sources / placeholders / coins / go
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
│   │   └── placeholders.py  # 基金占位页（铜币 2026-10-09 升级为正式模块，见 §18）
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

**迁移策略**：`schema_meta.schema_version` 从 `1` 起。`CREATE TABLE IF NOT EXISTS` 只能**新增表/索引**；**给老表加列**（SQLite 不支持 `ADD COLUMN IF NOT EXISTS`）走 `app/migrations/` 里按版本登记的迁移 —— `db.init_db` 在版本不一致时执行**幂等**的 `ALTER TABLE`（列已存在则跳过）再抬版本号，未登记的版本变动仍抛错要求人工处理。
> **v3 → v4 首次启用该机制**（2026-10-14）：`coin_series` 加 `comments`/`mints`、`coin_issues` 加 `catalog_no`（登记在 `app/migrations/v4.py`，同版本新增的 `coin_series_cn` / `coin_issues_cn` / `coin_api_usage` 是纯新增表，由 DDL 建好）。

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


## 11. 调度、运行与运维

### 11.1 启动流程（`app/main.py` lifespan）

```
1. logging_setup()                      # 日志先就位
2. config.load()                        # 校验失败 → 打印错误并 exit(1)
3. db.init_db()                         # PRAGMA + 建表 + schema_version 校验
4. repository.sync_sources_from_config()# YAML 源清单 → sources 表
5. scheduler.start()                    # AsyncIOScheduler（只注册 job，不立即执行）
6. yield（服务运行）
7. scheduler.shutdown()（关闭时）
```

**没有「启动补拉」这一步**（2026-10-15 取消，见 §19）：重启不抓取、不打分、不采集。
要立刻跑一次，去 `/control` 点「立即运行」，或用 CLI 命令。

### 11.2 任务清单

| job | 触发 | 说明 |
|---|---|---|
| `daily_pipeline` | 每天 `08:00` Asia/Shanghai | 按开关跑「抓取」与「打分」（§19） |
| `daily_coins` | 每天 `08:00` Asia/Shanghai | 按开关跑「采集」与「查价」；与知识日报互不传播失败（§18.9） |

- 两个 job 跑之前都读 `feature_switches`：对应开关关着就跳过，日志里能看到「已停用，跳过」（§19）。
- `misfire_grace_time=3600`：本机 08:05 才醒，任务仍然执行而不是被丢弃。
- `max_instances=1`：手工 CLI 与定时任务重叠时不并跑（`items` 的 UNIQUE 冲突只是兜底）。
- 「抓取与打分之间等 1 秒」只留在 `pipeline.run` 里（CLI 出报用）；定时任务拆成两步执行，不经过它。

### 11.3 CLI（`python -m app.cli <cmd>`）

| 命令 | 用途 | 对应里程碑 |
|---|---|---|
| `fetch [--source-id N] [--dry-run]` | 抓取（`--dry-run` 只打印解析结果不写库） | M1 调试 |
| `score [--limit N] [--rescore --prompt-version v1]` | 打分 | M2 校准 |
| `pipeline` | 抓取 + 打分 | 手工出报 |
| `sources` | 打印源状态表格 | M1 |
| `purge --before YYYY-MM-DD [--yes]` | 手工清理旧数据（spec §11 保留策略） | M6 |


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
| 13 | 历史搜索的时间范围过滤与结果排序都用 `items.published_at`（日报候选池仍用 `fetched_at`，两者口径**故意不同**） | spec §8 只说「关键词 + 时间范围 + 入口筛选」，没规定筛哪一列。M4 首版筛的是 `fetched_at`（与日报同口径），实测踩坑：筛「今天」会把 26 条**昨天发布、今天早上才抓到**的文章也列出来，而卡片上显示的是发布时间，看着就像筛错了（2026-09-23 用户提出）。凡页面显示哪个时间，筛选就该是哪个时间。**代价**：`published_at` 为 NULL 的条目在设了时间范围时搜不到（不设时间范围仍可搜到）—— 原本指美团那 10 条，已由第 14 条从 URL 反解补上，当前全库 0 条。日报继续用 `fetched_at`，因为它回答的是「今天新到的东西里哪些值得看」（第 7 条） |
| 14 | feed 完全不给时间时，从条目 URL 路径里的 `YYYY/MM/DD` 反解发布日期 | 美团技术团队的 feed 实测如此：`<item>` 里只有 `title`/`link`/`description`/`content:encoded`，没有 `pubDate`/`dc:date`（channel 级那个是 feed 生成时间，不是文章时间）。M1 记下了「该源 10 条 `published_at` 为 NULL」，M4 做搜索时暴露后果：这些条目在时间筛选下永远搜不到，卡片也从没显示过发布时间；用户核对文章页后发现**页面上有发布日期**（`2026-07-24`），而 URL 路径恰好是 `/2026/07/24/`，逐条对得上。故按 URL 反解，不抓详情页（plan §16 禁止浏览器渲染抓取，这不违反）。站点只给到「日」粒度，取当日 00:00Z，页面按东八区显示成同日 08:00，日期不漂。兜底只在 feed 无任何时间字段时触发，有真时间优先；数字凑不出合法日期仍为 NULL。这是解析兜底，不是 plan §16 禁止的「插件式数据源抽象」 |
| 15 | `startup_fetch` 在 `app/scheduler.py` 里定义，但由 lifespan 用 `asyncio.create_task` 直接调起，**不注册成 APScheduler 的 job** | §11.1 第 6 步写的是 `asyncio.create_task(pipeline.run())`（不 await），§11.2 的任务清单又把它列成一条 job（「启动后立即（一次性）」），两处措辞不一致。按 §11.1 实现：首轮抓取要尽早开始，走调度器反而多一层「等 `start()` 完成」的时序。**代价**：它的失败不会进 APScheduler 的 job 统计，所以它自己就地兜住异常并打堆栈 —— 否则 `create_task` 抛出的异常只会变成 `Task exception was never retrieved`，服务还在跑但没人知道这轮抓挂了。`daily_pipeline` 仍在调度器里，失败由 APScheduler 记录 |
| 16 | `cli purge` 的两点补充：① 不带 `--yes` 只预览（打印将删除条数）不删；② 切日按 `config.app.timezone`（复用 `day_bounds_utc`） | §11.3 只给了命令形式 `purge --before YYYY-MM-DD [--yes]`，没说 `--yes` 缺省时是交互确认还是直接删，也没说日期按哪个时区切。选预览而非交互确认，是为了在脚本里也安全；时区取业务时区，与日报/搜索同一口径，否则会出现「页面按东八区算 09-23、purge 却按 UTC 切日」的错位。实测（库副本）：`--before 2026-09-23` 预览 112 条且 `items` 保持 149，`--yes` 后 `items` 149 → 37、`item_scores` 147 → 35（外键级联）、`sources` 7 与 `fetch_runs` 25 不变；非法日期 `2026-13-99` 打印明确错误并返回退出码 1 |
| 17 | 新增 `tests/conftest.py`（`no_scheduler` 夹具）与 `tests/test_scheduler.py` | §4 的目录树只列了 4 个测试文件。`lifespan` 接上调度器后，`TestClient` 走 lifespan 的用例会真的去抓 6 个源，破坏 §13.1「测试不发真实网络请求」这条前提；故在 conftest 里把 `scheduler.start` 换成空壳（2026-10-15 起启动补拉已取消，`startup_fetch` 不再需要挡），由 `web` 这类夹具显式请求。`test_scheduler.py` 单独放调度器用例：它是 §13.1 之外的新增覆盖（触发点 08:00 Asia/Shanghai、`coalesce` / `misfire_grace_time=3600` / `max_instances=1`、错过 grace 就不补跑、启动补拉就地兜异常） |
| 18 | **放宽「不做浏览器渲染抓取 / 不做反爬对抗」——仅对铜币拍卖模块生效**（spec §14.3） | §16 的不做清单原写死这两条，前提是知识聚合只走 RSS。铜币拍卖的目标站（麦稀奇）实测是 JS 单页应用 + 阿里云滑块验证，直连只返回约 1.3KB 验证页（2026-10-09 实测），httpx 路径不可用。用户明确「修改需求，需要反爬」。故新增 §18 一章，仅该模块允许 Playwright 渲染与会话复用；**知识聚合一行未改**，验证码自动破解仍不做 |
| 19 | 新增依赖 `openpyxl` | spec §14.2 要读 `orders.xlsx`。`openpyxl` 只读一个工作表，不引 `pandas`（§2 的技术选型一贯「单用户无收益就不引重依赖」）。~~`playwright`~~ 已在 2026-10-12 改走 gapi 后移除 |
| 20 | 新增 `app/coins/*`、`app/routers/coins.py`、`config.yaml` 的 `coins:` 段、`data/mxiqi-profile/`（gitignore） | spec §14 要求一个与知识聚合**完全独立**的模块。放成 `app/coins/` 子包是「站点耦合只落一处」的落地方式（§18.2），不违反 §16「不做插件式数据源抽象」——那里禁止的是**可插拔的数据层抽象**，这里只有一个写死的实现 |
| 21 | `schema_version` 1 → 2（新增 `coin_*` 表，纯 `CREATE TABLE IF NOT EXISTS`，无 ALTER） | spec §14.5 新增 7 张 `coin_*` 表。§6.2 的迁移策略要求加列写 `migrations/vN.py`；本次只建新表、不动旧表，故只需在 `db.init_db()` 里补建并抬版本号 |
| 22 | **取消「启动补拉」**，新增功能开关与管理页 `/control`（`app/switches.py` / `app/tasks.py` / `app/routers/control.py`），`schema_version` 5 → 6（只新增 `feature_switches` 表） | spec §9 原本要求「服务启动时立即执行一次抓取」以覆盖休眠漏跑，但代价很直接：本机**经常重启**，每重启一次就真去抓一遍全部源、并把待打分条目重新喂一遍 LLM —— 那是整个系统唯一烧 token 的地方（2026-10-15 用户提出「现在需要经常重启，避免不必要的花费」）。改为：重启不做任何事；四个功能（抓取 / 打分 / 铜币采集 / 铜币查价）各有开关（存库、重启保留），控制每天 08:00 的定时任务；管理页另有「立即运行」（手动触发、忽略开关、后台执行）。spec §9/§14.8 与 plan §11.1/§11.2 已同步修订 |

除此之外，数据表字段与 spec §5 完全一致（仅补了必要索引与 `CHECK` 约束）；`coin_*` 七表的 DDL 见 §18.7。

> **2026-10-12 修订（覆盖第 18/19/20 条的浏览器相关部分）**：麦稀奇小程序网关 `gapi.mxiqi.com`
> 免登录、免签名即可取到专场 / 拍品 / 站内成交，故**整块删除**浏览器方案（Playwright、CDP、
> `data/mxiqi-profile/`、`coins-dump`、`blocked` / `need_login` 提示），依赖里的 `playwright` 也移除。
> 第 18 条「放宽反爬」的动机随之消失——现在只接公开接口。详见 spec §14.3 与 §18.3/§18.5。


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


## 16. 风险与对策（实现层）

| 风险 | 触发信号 | 对策 |
|---|---|---|
| 日报候选池条数不足（spec §11 目标 ≥30/天） | 连续 3 天候选 < 20 | 源数量不足是主因；按 §15 先把 bigdata 源补到 5 个以上，再考虑放宽 `lookback_hours` |
| LLM 与 OpenAI 协议不兼容 | 400/404 且非 token 问题 | `response_format` 自动降级；`base_url` 走配置可换厂商；保留原始响应 body 到日志 |
| LLM 调用成本超预期 | 日志中 token 累计 | 入池优先用「当日新增 + 未打分」，不重跑历史；摘要截断 1500 字 |
| 打分质量差（理由空泛） | 人工读 reasons 无法反驳 | 只改 §8.2 的 prompt 并递增 `prompt_version`，用 `cli score --rescore` 对比新旧理由，不靠感觉 |
| 源集体失效 | 源管理页大面积标红 | 单源失败已隔离；把源清单当配置维护，失效源改为 `enabled: false` 而非删除（保留历史 `fetch_runs`）。从 `sources.yaml` 删掉的源由 `sync_sources_from_config()` 在下次启动时自动停用，不需要手工改库 |
| SQLite 写冲突 | `database is locked` | WAL + `busy_timeout=5000` + 写操作集中在 `to_thread`；若仍出现，改为全局 `asyncio.Lock` 串行写 |
| 服务未启动导致漏天 | 日报页显示「非今日数据」 | 不做开机自启、不做启动补拉，接受漏天（spec/core.md §9）；错过 1 小时内由 `misfire_grace_time=3600` 补跑一次 |

**明确的「不做」清单（防止实现时膨胀）**：不做用户体系、不做点赞/踩、不做 AI 自动发现源、不做标题相似度去重、不做调仓/交易建议、不做插件式数据源抽象、不做自动数据清理、不做验证码自动破解。

> **不做清单的边界**：「不做浏览器渲染抓取 / 不做反爬对抗」对**两个模块都成立** —— 铜币曾短暂豁免（2026-10-09~10-12 的 Playwright + 会话复用方案），后因麦稀奇小程序网关免登录、免签名且免浏览器而**整块删除**（spec/coins.md §14.3、plan/coins.md §18.3）。清单其余各项同样对两个模块成立。

## 17. 需要你拍板的 3 个点

1. **日报条数**：默认 8 条（区间 5-10）。若你希望固定 10 条，改 `digest.limit` 即可。
2. **打分并发与重试**：默认并发 3、失败重试 1 次。若你的厂商 API 有严格 RPM 限制，需下调并发。
3. **服务端口**：默认 `8765`（避开 8000/3000 常见占用）。若与本机其他服务冲突请指定。

其余参数均已按 spec §11 的你的设定落进 `config/config.yaml`（08:00 出报、全量保留 + 手动 purge、`deepseek-v4-flash`）。


## 19. 运行控制与功能开关（v1.6 增补；对应 spec §9）

### 19.1 为什么要有这一章

原方案（§11.1）启动时用 `create_task` 跑一轮补拉，目的是覆盖本机休眠漏跑。但本机**开发期经常重启**，
每重启一次都会：把全部源真抓一遍、把待打分条目再喂一遍 LLM（整个系统唯一烧 token 的地方）、铜币再采一轮并查价。
2026-10-15 用户要求：**重启不做任何事**，改成「要跑就手动跑或定时跑，且能按功能关掉」。

### 19.2 开关模型

四个功能各有独立开关（key 即 `feature_switches.key`）：

| key | 分组 | 功能 | 花 token |
|---|---|---|---|
| `knowledge_fetch` | 知识聚合 | 抓取所有启用的源 | 否 |
| `knowledge_score` | 知识聚合 | 给待打分条目打分 | **是**（唯一） |
| `coins_collect` | 铜币拍卖 | 采集专场与拍品（gapi） | 否 |
| `coins_price` | 铜币拍卖 | 查站内历史成交 | 否 |

- **只影响每天 08:00 的定时任务**；`/control` 的「立即运行」是手动触发，**忽略开关**。
- 默认全部启用（`app/switches.py` 的 `DEFAULT_ENABLED`）；库里没有记录的功能按默认值算，
  **不往库里写默认值** —— 这样以后改默认值能直接生效。
- 未知 key 一律当「未启用」，避免拼错 key 反而静默跑起来。

### 19.3 数据层

`feature_switches` 表（`schema_version` 5 → 6；纯新增表，`FORWARD_ADDITIVE_UPGRADES` 里登记 `("5","6")`）：

```sql
CREATE TABLE IF NOT EXISTS feature_switches (
  key        TEXT PRIMARY KEY,   -- knowledge_fetch / knowledge_score / coins_collect / coins_price
  enabled    INTEGER NOT NULL,
  updated_at TEXT    NOT NULL    -- UTC ISO8601
);
```

repository 两个接口：`feature_switch_states()`（读全部 → `dict[str, bool]`）、
`set_feature_switch(key, enabled)`（upsert）。

### 19.4 执行入口（`app/tasks.py`）

四个功能各一个「跑一次 → 返回一句摘要」的函数，**定时任务与管理页共用**：

| 函数 | 做什么 |
|---|---|
| `run_knowledge_fetch` | `fetcher.run_once()` |
| `run_knowledge_score` | `scorer.run_pending()` |
| `run_coins_collect` | `coins_pipeline.run(collect_only=True)` |
| `run_coins_price` | `coins_pipeline.run(price_only=True)` |

手动触发走 `trigger(config, key)`：`asyncio.create_task` **后台执行**（铜币查价一轮可能几分钟，不能挂住 HTTP 请求），
同一功能已在跑时返回 `False`（连点不叠加），跑完把 `RunState(status, message, finished_at)` 记进 `last_state` 供页面显示。

> `pipeline.run`（抓取 + 打分一体）保留给 CLI `pipeline`；定时任务拆成两步走，见 §11.2。

### 19.5 页面与路由（`app/routers/control.py`）

| 路由 | 动作 |
|---|---|
| `GET /control` | 四个功能的开关状态 + 运行状态（运行中 / 空闲）+ 最近一次结果 |
| `POST /control/{key}/toggle` | 启停取反（302 回本页）；未知 key → 404 |
| `POST /control/{key}/run` | 立即跑一次（302 回本页，后台执行）；未知 key → 404 |

`run` 必须是 `async def`：`trigger` 里要 `create_task`，同步路由跑在线程池里没有事件循环。
导航栏在「源管理」后新增「运行控制」入口。

### 19.6 测试

- `tests/test_control.py`：默认全开、toggle 落库（重读仍保留）、未知 key 当未启用、页面列出四个功能、
  启停 302 与状态翻转、未知功能 404、「立即运行」忽略开关且后台跑完把结果留在页面上。
- `tests/test_scheduler.py`：`daily_pipeline` 按开关执行（关掉抓取只打分、两个都关整轮无事）、
  抓取失败仍向上抛给 APScheduler、`daily_coins` 采集与查价都关时跳过、网关失败仍就地兜住。
- `tests/conftest.py`：`no_scheduler` 只挡 `scheduler.start`（补拉已不存在）。

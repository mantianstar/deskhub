# 知识聚合 — 技术方案

> 本文件是 [plan.md](../plan.md) 的**知识聚合模块**部分，编号沿用 `§7`~`§10`、`§14`、`§15`。
> **跨文件编号**：`§1`~`§6`、`§11`~`§13`、`§16`、`§17`、`§19` → [core.md](./core.md)；`§18.x` → [coins.md](./coins.md)。
> **变更过程与理由**见 [CHANGELOG.md](../CHANGELOG.md)。

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
| `/coins` | GET | ~~铜币拍卖占位（「未启用」）~~ → **已升级为正式模块，路由见 §18.8** | `coins/sessions.html` |
| `/healthz` | GET | JSON：`{db, sources_total, sources_failing, last_fetch_at}` | — |

**模板约定**

- `base.html`：导航栏（日报 / Agent / 大数据 / 历史搜索 / 源管理 / 基金 / 铜币）+ 内容块 + 一行原生 JS。
- `partials/item_card.html`：`标题 / 来源 / 打分 / 理由 / 时间`，被 digest、module、search 三处复用（保证同一套视觉）。
- 原生 JS 只做两件事：搜索页的「展开高级筛选」、点击外链时置灰防重复点。无框架、无 CDN。
- 打分视觉分级：`>=75` 绿、`50-74` 灰、`<50` 浅色、无分「未评分」。颜色语义写在 CSS 变量里，便于后面调。


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
| **M7 铜币·画像** | `coins/profile.py`、`repository.py`（`coin_*`）、`routers/coins.py`（画像页）、`orders.xlsx` 解析 | `orders.xlsx` → `coin_orders` + `coin_profile_tags`；画像可编辑 | spec §14.12 第 1 条；离线 fixture 用例 |
| **M8 铜币·采集** | `coins/gapi.py`（小程序网关）、`app/coins/pipeline.py`、`cli coins-fetch` | 能拿到全部当前专场 + 拍品并入库 | spec §14.12 第 2 条；离线 payload 解析用例；请求参数用例 |
| **M9 铜币·推荐与页面** | `coins/matcher.py`、`coins/pricing.py`、`routers/coins.py`、`templates/coins/*` | 两个页面（专场列表 / 拍品页）+ 推荐价取证 | spec §14.12 第 3/4/5 条 |
| **M10 铜币·收口** | `scheduler.py`（`daily_coins`）、`tests/`、导航改正式入口 | 每天 08:00 独立任务；测试全绿 | spec §14.12 第 6 条起算；`pytest` 通过 |

**M1 的额外动作**：把每个源的原始 XML 存到 `tests/fixtures/`，后续所有解析改动都靠离线样本回归，不在测试里访问真实网络。


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

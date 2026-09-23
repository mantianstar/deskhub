# AI 个人工作台 — tasks.md

> 依据：[spec.md](./spec.md) v1 + [plan.md](./plan.md)。
> 本文件只做**任务拆解与排序**，不新增 spec/plan 之外的能力。
> 里程碑顺序严格照 spec §13 / plan §14：**先内容质量，后外壳**。顺序搞反会得到一个漂亮的空壳。

## 0. 使用说明

- 任务按 `M0 → M6` 顺序执行，同一里程碑内按编号执行；**依赖列**标了前置任务，未满足不得开工。
- 每完成一项，勾选 `[x]` 并在「验证」列留下证据（命令输出 / 截图 / 人工结论）。
- 验证失败不进入下一个里程碑；测试用例编号对应 plan §13.1。
- 涉及外部不可控因素（源可达性、LLM 厂商兼容性）的任务，**必须实测**，不得凭猜测打勾。
- **提交粒度与推送时机**：以「一个能说清为什么的完整改动」为单位提交（不做无意义的小碎步，也不把多件事混在一笔），代码与对应文档同一次提交；**每天结束或功能完成时** `git push origin main`（远端 `git@github.com:mantianstar/deskhub.git`）。commit message 用中文说清「为什么」，**精简成一句话**，不罗列改了哪几行，也不写大段 body（2026-09-23 明确）。

**全局禁止项**（plan §16 明确不做，实现时反复对照）

不做用户体系 · 不做点赞/踩 · 不做 AI 自动发现源 · 不做浏览器渲染抓取 · 不做标题相似度去重 · 不做调仓/交易建议 · 不做插件式数据源抽象 · 不做自动数据清理 · 不接入公众号/知乎/即刻/小红书 · 不做基金/铜币的业务逻辑。

---

## M0 — 骨架 — 已完成

目标：服务能起来，库表能建，`/healthz` 有响应。此时**不写任何抓取与打分逻辑**。

| # | 任务 | 产出文件 | 依赖 | 验证结果 |
|---|---|---|---|---|
| [x] M0-1 | 初始化目录结构（`app/`、`app/routers/`、`app/templates/`、`app/static/`、`config/`、`data/`、`tests/fixtures/`）与依赖清单 | `requirements.txt`、`.gitignore`、`.env.example`、`README.md` | — | `.venv/bin/pip install -r requirements.txt` 成功（Python 3.14.7，fastapi 0.141.1 / uvicorn 0.53.0） |
| [x] M0-2 | 写配置模板：应用/日报/抓取/打分/LLM/模块关注点；源清单先放 1 条占位 | `config/config.yaml`、`config/sources.yaml` | M0-1 | 结构与 plan §5.1 / §5.2 一致；源清单暂放 Hacker News，余下 4 个源留待 M1-6 实测后补入 |
| [x] M0-3 | 实现配置加载与校验：`环境变量 > .env > config.yaml`；`sources.yaml` 中 url 重复即报错退出；`api_key` 为空时打印警告但允许启动 | `app/config.py` | M0-2 | 重复 url → 拒绝启动并指出冲突的两条；未知 module → 拒绝启动；缺 key → 仅 WARNING |
| [x] M0-4 | 日志初始化：格式 + `RotatingFileHandler` 写 `data/logs/deskhub.log` | `app/logging_setup.py` | M0-3 | `data/logs/deskhub.log` 已生成，启动四条日志齐全 |
| [x] M0-5 | DDL 常量 + dataclass（`Source`/`Item`/`ItemScore`/`FetchRun`）+ 连接工厂（PRAGMA：WAL / foreign_keys / busy_timeout）+ 建表 + `schema_meta.schema_version=1` 校验 | `app/models.py`、`app/db.py` | M0-3 | 首次启动生成 `data/deskhub.db`，`items`/`item_scores`/`sources`/`fetch_runs`/`schema_meta` 五表齐全，`schema_version=1` |
| [x] M0-6 | repository 基础函数：`utcnow()` / `parse_dt()`（统一 UTC ISO8601）、`sync_sources_from_config()`（按 url upsert，保留运行时状态）、`list_sources()`、`set_source_enabled()`、健康检查计数 | `app/repository.py` | M0-5 | 临时库验证：重复 sync 不增行、`fail_count`/`last_ok_at`/`created_at` 保留、`name`/`enabled` 随配置更新；`fail_count>=3` 计数为 1；时间工具对坏值/空值返回 None |
| [x] M0-7 | FastAPI 装配：lifespan（log → config → db.init → sync_sources → 留出 scheduler/补拉钩子）、路由注册点、全局异常处理器；实现 `/healthz` | `app/main.py` | M0-6 | `uvicorn app.main:app` 起服务，`curl /healthz` 返回 `{"db":"ok","sources_total":1,"sources_failing":0,"last_fetch_at":null}` HTTP 200 |

**M0 完成判据：已满足** —— `uvicorn app.main:app` + `curl 127.0.0.1:8765/healthz` 返回合法 JSON，`data/deskhub.db` 建表成功。

实测补充（plan 未写明，留给后续里程碑决策）：

- 配置校验失败时进程确实拒绝启动（`Application startup failed. Exiting.`），但因异常发生在 lifespan 内，uvicorn 把 `SystemExit(1)` 转成了**退出码 3**。若必须严格等于 1，需把 `config.load()` 提到模块导入期，与 plan §11.1 的启动顺序有冲突，故未改。
- `sync_sources_from_config()` 对已存在的源**以配置为准覆盖 `enabled`**：界面上的临时启停会在下次启动时被 `sources.yaml` 覆盖（plan §16 要求把源清单当配置维护）。若希望界面启停长期生效，M5 需改为「DB 优先」。

---

## M0 修订记录

### R1（2026-09-22）：配置删源自动停用

**触发**：运行期发现把 `config/sources.yaml` 换成 6 个中文源后，`sources` 表里 M0-2 留下的 Hacker News 仍 `enabled = 1`。根因是 `sync_sources_from_config()` 只做 upsert、从不处理「配置里已删除的源」，被删的源会永久留在表里并继续被抓。

| # | 任务 | 产出文件 | 依赖 | 验证结果 |
|---|---|---|---|---|
| [x] R1-1 | `sync_sources_from_config()` 增加「配置里已不存在的源自动置 `enabled = 0`」，**不删行**（保留历史 `fetch_runs` / `items` 关联，符合 plan §16） | `app/repository.py` | M0-6 | 实跑 sync 后 Hacker News `enabled=0`、6 个中文源 `enabled=1`、总行数 7 不变 |
| [x] R1-2 | 起头数据层测试文件（临时 db 跑真实 SQL） | `tests/test_repository.py` | R1-1 | `.venv/bin/python -m pytest tests/test_repository.py -q` → 3 passed |
| [x] R1-3 | 按层级回写文档（spec §2.3；plan §5.2 / §6.4 / §12 第 10·11 条 / §13.1 / §16） | `spec.md`、`plan.md` | R1-1 | 差异已登记在 plan §12，spec 只补实测结论、未改准入类别 |

**记录订正**：M0-2 的「源清单暂放 Hacker News」已被本次修订取代（原文保留不改，那是当时的真实状态）；M6-4 的 `tests/test_repository.py` 由 R1-2 起头，M6-4 只需按 plan §13.1 补全其余用例。

---

## M1 — 抓取管道（先蹚管道）

目标：真实源能抓到条目并入库，失败可分类、可追溯。

| # | 任务 | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|
| [x] M1-1 | 实现 `canonicalize(url)` + `url_hash`：scheme 统一 https、host 小写去 `www.`、去 fragment、去跟踪参数（`utm_*`/`fbclid`/`gclid`/`ref`/`source`/`spm`/`from`）、路径去尾斜杠与重复斜杠、query 按 key 排序 | `app/fetcher.py` | M0 | plan §13.1 用例 1：`tests/test_url_hash.py` 30 passed，覆盖去跟踪参数 / `www.` / 尾斜杠 / `#` / host 大小写 / query 排序 / 默认端口 / IPv6 / 协议相对写法 / 6 种非法 URL 抛错；同一文章 4 种写法 hash 相同、不同文章 hash 不同 |
| [x] M1-2 | 单源抓取流程：`httpx.AsyncClient(timeout=10, follow_redirects=True, headers=UA)` → `feedparser.parse` → 按发布时间降序取前 `max_items_per_source` 条 → 逐条规范化 URL / 解析时间 / 清理摘要（去标签截断 2000 字）→ 入库 | `app/fetcher.py` | M1-1 | `cli fetch --dry-run` 打印 6 个源解析结果：InfoQ 20 条 / 掘金 20 条 / 量子位 10 条 / 博客园 20 条 / 开源中国 30 条（feed 实有 50 条，按上限截断）/ 美团 10 条；无 link 的条目被跳过 |
| [x] M1-3 | 失败分类与源状态机 + `fetch_runs` 落库：`ok` / `empty` / `http_error` / `timeout` / `parse_error`；成功清零 `fail_count` 并更新 `last_ok_at`，失败 `+1`；单源整体超时 `wait_for(timeout+5)` | `app/fetcher.py`、`app/repository.py` | M1-2 | plan §13.1 用例 3/4/5 通过（`tests/test_fetcher.py`）：500 → `http_error`+`fail_count=1`+`last_ok_at` 不变且同轮其他源照常入库；空 feed → `empty`+`fail_count` 不变+`last_ok_at` 更新；坏 XML → `parse_error`；`ConnectTimeout` → `timeout`；连败 3 次 → `count_failing_sources()=1`，成功后归零 |
| [x] M1-4 | repository 补齐幂等写入：`insert_item_if_new()`（靠 `url_hash` UNIQUE 冲突返回 `None`）、`record_fetch_run()`、`mark_source_ok()`、`mark_source_fail()`；`items` 插入与 `fetch_runs` 写在同一事务 | `app/repository.py` | M1-3 | plan §13.1 用例 2 通过；真实库连抓两次：第一次新增 110 条，第二次全部 `新增=0` 且 `items` 总数不变；第三次只新增源在这几分钟里真正新发的 2 条（去重没有误杀）；4 个写函数共用 `save_fetch_result()` 的同一连接，任一步失败整轮回滚 |
| [x] M1-5 | CLI：`fetch [--source-id N] [--dry-run]`、`sources`（打印源状态表格） | `app/cli.py` | M1-4 | `python -m app.cli fetch` 后有真实条目入库；`cli sources` 打印 7 行（含已停用的 Hacker News），`last_ok_at` 与 `fail_count` 两列可读（中英混排按显示宽度对齐）；`--source-id` 可绕开启停单抓 |
| [x] M1-6 | 按 plan §15 逐条写首批源（agent ≥3、bigdata ≥2），**每个 url 用 `--dry-run` 实测 `http_status` 与条目数**，不可达的直接换掉，不凑数 | `config/sources.yaml` | M1-5 | 6/6 源 `status=ok`、`http_status=200` 且条目数 >0（20/20/10/20/30/10），超过「5 中 ≥4」的判据，无需替换 |
| [x] M1-7 | 把实际抓到的原始 XML 存成离线样本 | `tests/fixtures/*.xml` | M1-6 | 6 个样本共 368K（`infoq/qbitai/juejin/oschina/meituan/cnblogs`），`feedparser.parse` 直接解析：entries=20/10/20/50/10/20、`bozo=False` |
| [x] M1-8 | 测试：URL 规范化边界穷举 + `respx` 模拟 HTTP（覆盖超时/500/空 feed/重复抓取） | `tests/test_url_hash.py`、`tests/test_fetcher.py` | M1-7 | `.venv/bin/python -m pytest tests -q` → **45 passed in 0.47s**（url_hash 30 + fetcher 12 + repository 3），全程 respx 拦截，无真实网络访问 |

**M1 完成判据：已满足** —— `sqlite3 data/deskhub.db "select count(*) from items"` = 112（>0，首轮 110 + 后续新发 2 条）；`cli sources` 可见 `last_ok_at` 与 `fail_count`。（spec §10 第 1 条）

实测补充（plan 未写明，留给后续里程碑决策）：

- **美团技术团队的 feed 没有单条发布时间**：`<item>` 里只有 `title/link/description/content:encoded`，没有 `pubDate`/`dc:date`，故 `published_at` 存 `NULL`。影响是它在日报排序里落到当日末尾，且 M2 的 prompt 会拿到「发布时间未知」。源本身可用（200 / 10 条、内容质量高），暂不替换；若 M2 校准发现时效性分数失真，再考虑换源或从 URL 的 `/2026/09/22/` 反解日期。**该议题已在 M1 修订记录 R1 处理**：按 URL 路径里的日期反解，源、解析与历史数据都已修好，此处的「暂不替换 / 再考虑」已被取代。
- **`--dry-run` 仍会按配置同步源清单**（否则拿不到 `source_id` 无法选中源）；它保证的是不写 `items` / `fetch_runs` / 源状态，不是「完全不碰库」。
- **未预期异常按 `parse_error` 落库并打完整堆栈**：`plan §1` 铁律 2（单源失败不影响整体）与铁律 4（不吞异常）在此处冲突，处理方式是「隔离 + 落库 + `logger.exception`」，不新增第 6 种 status。数据库写失败在一源处理链的 try 外，仍会向上抛出，不会被误判成源故障。
- **`empty` 不等于故障也不洗白历史**：源返回合法 feed 但 0 条时更新 `last_ok_at` 但保留 `fail_count`，避免「长期无更新的源被标红」与「一次空响应清掉连败记录」两个方向的误判。

---

## M1 修订记录

### R1（2026-09-23）：美团源发布时间从 URL 路径反解

**触发**：M4 做完搜索后暴露后果 —— 这 10 条 `published_at` 为 NULL 的条目**在设了时间范围时永远搜不到**，卡片也只能显示「发布时间未知」。用户核对美团文章页，发现**页面上有发布日期**（截图：`2026-07-24`），要求修复。

**核实（先证伪，再定修法）**：feed 的 `<item>` 确实没有任何时间字段（只有 channel 级 `pubDate`，那是 feed 生成时间，M1 的判断没错）；但**链接路径里带日期**，且与文章页逐条对得上：

```
https://tech.meituan.com/2026/07/24/LongCat-MineExplorer.html   ↔ 文章页 2026-07-24
https://tech.meituan.com/2026/09/22/Meituan-Foundation-...html  ↔ 文章页 2026-09-22
```

故按 URL 反解，不去抓详情页（plan §16 禁止浏览器渲染抓取，这条不违反）。

| # | 任务 | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|
| [x] R1-1 | feed 无任何时间字段时，从链接路径反解 `YYYY/MM/DD`；有真时间优先，凑不出合法日期仍为 NULL | `app/fetcher.py` | M1-2 | `tests/test_fetcher.py` 新增 3 条：真实样本 `meituan.xml` 解析后 10/10 条都有发布时间、最新一篇 = `2026-09-22T00:00:00Z`、抽查 `/2026/07/24/` 那条 = `2026-07-24T00:00:00Z`；feed 自带 `pubDate` 时 URL 里的日期被忽略；边界 `/2026/13/45/`、`/2026/02/30/`、`/2026/09/12345/`、无日期路径、空串、`None` 全部返回 NULL |
| [x] R1-2 | 回填库里已有的 10 条 NULL | — | R1-1 | 实跑回填：10 条全部写入（`2026-07-24` ~ `2026-09-22`），全库 `published_at IS NULL` 计数 **10 → 0**；id 110 = `2026-07-24T00:00:00Z`，与文章页显示的 2026-07-24 一致 |
| [x] R1-3 | 页面复核 | — | R1-2 | 真实服务：`/search?q=MineExplorer` 卡片显示「07-24 08:00 发布」（不再是「发布时间未知」）；`/search?start=2026-07-24&end=2026-07-24` 命中 2 条（两篇 LongCat 文章），与库内一致 |
| [x] R1-4 | 按层级回写文档 | `plan.md`、`tasks.md`、`README.md` | R1-3 | plan §12 新增第 14 条，第 13 条的代价说明由「美团 10 条」改为「当前 0 条」；M4 修订记录 R1 的代价记账同步；README 去掉「如美团」这个已失效的例子 |

**记账（本次不做）**：这 10 条的打分是在「发布时间未知」的前提下算出来的，是否重打 —— **建议不重打**：实测它们当前分数是 10~33（全部 < 50，离日报前 8 名的门槛 34 分还差得远），时效性那 20 分既不可能把它们抬进日报也不会改变任何筛选结果；真要重打只能 `cli score --rescore`（按 prompt 版本整批删，注意 M2 记录的坑）。测试回归：`.venv/bin/python -m pytest tests -q` → **101 passed**。

---

## M2 — LLM 打分（理由必须可反驳）— 已完成

目标：每条条目有 `score` + `reason`，且理由具体到「这条讲了什么、为什么和我有关」。

| # | 任务 | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|
| [x] M2-1 | LLM 抽象层：`LLMClient` 协议 + `OpenAICompatClient`（`POST {base_url}/chat/completions`，Bearer 鉴权，`response_format=json_object` 遇到 400 自动降级并缓存标记）+ `FakeClient`（测试注入，可返回坏 JSON） | `app/llm.py` | M0-3 | 真实 key 打通：`POST https://api.deepseek.com/chat/completions` 连打 112 条全部 200，日志无降级 WARNING（该厂商支持 `response_format`）；降级分支改由 `tests/test_llm.py` 用 respx 覆盖（见下方「记录订正」） |
| [x] M2-2 | prompt v1 组装：system 含 `module_label` / `module_focus` / 三段打分口径（相关度 50 / 信息密度 30 / 时效性 20）；user 含标题、来源、发布时间、截断 1500 字摘要 | `app/scorer.py` | M2-1 | `test_prompt_matches_plan_and_truncates_summary` 逐条断言 plan §8.2 的关键句（三段口径 / 禁空话 / 摘要截断到 1500）；`test_prompt_handles_missing_summary_and_published_at` 覆盖无摘要、无发布时间 |
| [x] M2-3 | 输出校验与兜底：剥 ``` 围栏、取第一个 `{...}` → `json.loads` → `score` clamp 到 [0,100]（`"95"` 转 95）→ `reason` 去空白且 ≥10 字；任一步失败重试 1 次（附加「只输出 JSON」）；仍失败**不写 `item_scores`**，记 WARNING，条目留池下轮重试 | `app/scorer.py` | M2-2 | plan §13.1 用例 6/7 通过：`"150"→100`、`"-20"→0`、`'"95"'→95`；6 种非法输出（非 JSON / 坏 JSON / 缺 score / score 非数字 / reason 过短 / 缺 reason）全部抛错；坏 JSON 第二次成功则正常落库且重试 prompt 带「只输出 JSON」；两次都坏 → `item_scores` 0 行且条目仍在待打分池 |
| [x] M2-4 | repository 补齐：`list_pending_score_items(limit, lookback_days=7)`（`LEFT JOIN item_scores WHERE item_id IS NULL`）、`upsert_score()` | `app/repository.py` | M2-3 | 真实库：打分前 `pending=112` → 打完 `pending=0`、`item_scores=112`、`items=112`，三者一致；`test_pending_pool_excludes_scored_and_outdated_items` 证明已打分的与超 9 天的都不再入池（窗口放宽到 10 天才回来） |
| [x] M2-5 | 并发与成本：`asyncio.Semaphore(3)`，每条独立重试互不影响；本轮结束打印 `items_scored` / `items_failed` / 累计 token | `app/scorer.py` | M2-4 | 真实日志：`本轮打分待打分=112 成功=112 失败=0 token(prompt=47786 completion=39361 total=87147)`，91 秒跑完；`test_concurrency_is_capped_by_config` 用探针客户端断言峰值并发恰为 3（6 条也压到 3） |
| [x] M2-6 | CLI：`score [--limit N]`、`score --rescore --prompt-version v1`（删该版本打分后重跑） | `app/cli.py` | M2-5 | 真实重跑：`重打分：已删除 prompt_version=v1 的 112 条旧打分` → 重新 112 行，`select item_id from item_scores group by item_id having count(*)>1` 返回 0 行；`--prompt-version` 不带 `--rescore` 时报错退出码 1（不静默忽略）；池空时 `score` 打印 0 条且不发任何请求 |
| [x] M2-7 | **人工校准**：随机读 10 条 `reason`，逐条判断**是否可反驳** | — | M2-6 | 10/10 可反驳，无需改 prompt（结论与样本见下方「M2 校准记录」） |
| [x] M2-8 | 测试：`FakeClient` 注入，覆盖坏 JSON 重试、clamp、reason 过短、全失败不落库 | `tests/test_scorer.py` | M2-7 | `.venv/bin/python -m pytest tests -q` → **76 passed**（url_hash 30 + fetcher 12 + repository 3 + scorer 21 + llm 10），全程无真实网络访问 |

**M2 完成判据：已满足** —— `cli score` 打完 112 条后人工随机读 10 条理由，10 条全部可反驳。（spec §10 第 2 条）

---

## M2 校准记录（M2-7 的原始证据）

随机抽 10 条（`order by random()`），逐条判断「能不能被反驳」——判定标准是：理由是否说清了「这条讲了什么 + 与关注点什么关系」，能不能拿原文指出它对或错。空话（「内容优质」「值得一读」）、复述标题一律算不合格。

| # | module / 分 | 标题（截断） | reason（截断） | 可反驳 |
|---|---|---|---|---|
| 1 | agent / 12 | 华为云码道面向鸿蒙开发者升级：上线鸿蒙编码大模型 | 属 IDE 级代码助手，未涉及 Agent 框架、规划或多智能体，且无技术细节可复用 | ✅ |
| 2 | bigdata / 3 | 禅道开源版 22.6 发布 | 项目管理工具，只更新需求批量编辑等界面细节，与 Hadoop/Spark/湖仓及数据调度、治理无关 | ✅ |
| 3 | agent / 33 | 《2026 年中国人工智能计算力发展评估报告》发布 | 只发布算力报告并给宏观预测，无框架、方法或落地细节，对 Agent 开发读者只有间接行业参考 | ✅ |
| 4 | bigdata / 2 | NeoVim 使用笔记 | 编辑器配置与 LSP 插件笔记，与大数据关注点完全无关 | ✅ |
| 5 | agent / 42 | 国产数据库跑出 AI 新能力！OceanBase 登顶国际 Data Agent 榜单 | 属 Agent 评测类动态，与关注点相关，但未披露方案架构、指标或复现细节，信息量有限 | ✅ |
| 6 | bigdata / 3 | 灵界 OS 5.0 稳定版 · 开箱教程上线 | 只新增开箱引导、语言选择等交互流程，与关注点完全无关 | ✅ |
| 7 | agent / 22 | 70 强项目观察之具身未来 | 摘要为空（正文只写「点击查看原文」），无法判断是否涉及规划、工具调用或记忆，几乎无可提取信息 | ✅ |
| 8 | bigdata / 16 | cloudflared，不需要服务器和公网 IP 的免费内网穿透 | 网络/运维工具用法，与大数据栈无直接关联，也无架构或调优细节 | ✅ |
| 9 | agent / 25 | 小米发布并开源 MiMo-V2.6 系列 | 属基础模型发布而非 Agent 框架或方法；读者仅在选底座模型时可能参考，缺少智能体技术细节 | ✅ |
| 10 | agent / 70 | 腾讯开源了一个项目，让 AI 直接用你已经登录好的浏览器 | 腾讯开源 BrowserSkill，让 Agent 复用本地已登录会话，属浏览器操作类工具动态，对做 agent 工具接入与实战的读者有直接参考价值 | ✅ |

**结论**：prompt v1 的「说清是什么 + 与关注点什么关系」这个结构真的被模型执行了——10 条里没有一条是「内容优质」式空话，也没有复述标题；低分条目还会明确写出「为什么无关」。故**不改 plan §8.2、不递增 prompt_version**。

顺带看到的两点（不是 M2 判据，留给 M3 决策）：

- **高相关条目的判定是准的**：≥50 分的 9 条正好是 LangGraph RAG 按需检索、LangGraph4j Multi-Agent Supervisor、快手分销增长 Agent 实践、AGENTS.md 上下文策略、菜鸟 AI Coding 复盘这类真内容，说明排序信号可用。
- **分数分布偏左**：112 条里 81 条 <30 分（≥50 分只有 9 条），因为源里混了大量与两个关注点无关的通用新闻（NeoVim 笔记、禅道发布、灵界 OS……）。这正是 spec §1 的首要风险在数据上的样子，M3 做日报时要接受「精选可能凑不满 8 条」，别靠抬分填满。

---

## M2 实测补充（plan 未写明，留给后续里程碑决策）

- **同 prompt 重跑的分数不稳**：temperature 0.2 下，同一批 112 条连跑两轮，分数完全一致的只有 18 条，平均绝对偏差 6.2 分，14 条差 ≥15 分（最大 `85 → 35`）；不过重跑前 ≥50 分的 9 条里有 7 条重跑后仍在 ≥50，**头部是稳的、中段会漂**。影响：M3 日报若要「同一天反复重打分」，列表会跳。M3 可选对策（届时二选一，不现在改）：把 `scoring.temperature` 调到 0；或坚持「每天只对当日新增打分、不重跑历史」。
- **真实成本比 plan §8.4 的估算高**：plan 按「prompt ≈700 in + 80 out」估，实测 112 条是 prompt 47786（≈427/条）+ completion 39361（≈351/条）= **87147 tokens/轮**，91 秒（并发 3、单条 ≈2.4s）。输出 token 超出预期是因为 prompt 要求 reason 20-60 字、模型还会带上少量结构文字。M6-3 核对账单时要按实测值而不是估算值看。
- **DeepSeek 的 `base_url` 不带 `/v1` 也能通**：`config.yaml` 写 `https://api.deepseek.com`，代码拼成 `/chat/completions`，112 次全 200；`response_format=json_object` 被接受，未触发降级。**换厂商前先用 `cli score --limit 3` 打一次**，能同时验通「域名 + 模型名 + json_object 支持」三件事，比 curl 更贴近真实调用路径。
- **`--rescore` 是按版本整表删，不是按条删**：`score --rescore --prompt-version v1 --limit 5` 会先删掉**该版本的全部** 112 条，再只补 5 条，剩下 107 条会退回未评分池。要重跑就整批重跑（`--limit` 给足），别用 `--limit` 配合 `--rescore` 做抽样。
- **打分失败不留痕**：失败的条目既不写 `item_scores` 也不落任何计数表，只在日志里留 WARNING（plan §8.3 就是这么定的）。好处是重跑不重复计费；代价是「某条一直打不上分」只能靠翻日志发现。若 M6 发现这种情况变多，再考虑加一张失败记录表，现在不加。

### 记录订正

- M2-1 的验证原文是「降级分支被 FakeClient 覆盖」——实际做不到：降级逻辑在 `OpenAICompatClient` 里，FakeClient 直接绕过整个 HTTP 层。已改为用 `respx` 在 `tests/test_llm.py` 里直接驱动 `OpenAICompatClient`：断言首次请求带 `response_format`、400 后第二次不带、且**第三次（新条目）也不再带**（证明标记被缓存），另覆盖 400 二次失败 / 5xx / 非 JSON 响应 / `choices` 为空 / `content` 空白 / 连接超时 6 种失败形态。
- plan §8.1 的协议签名 `complete_json(system, user) -> str` 未改，但客户端另加 `usage` 属性（累计 token，供 M2-5 打印成本）与 `aclose()`（一个 run 共用一个 httpx 连接池，结束后由 scorer 关闭）。这是实现细节，不影响 plan 描述的分层。

---

## M3 — 日报首页 — 已完成

目标：首页能看到当日 5-10 条精选，点击可跳转且被记录。

| # | 任务 | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|
| [x] M3-1 | pipeline 编排：`fetch → sleep(1)（仅日志可读）→ score`，供调度器与 CLI 共用 | `app/pipeline.py`、`app/cli.py` | M2 | `cli pipeline` 真实跑通：6 个源（量子位 `timeout`，其余 5 个 `ok`）新增 35 条 → 间隔 1s → 打分 35 条全成功、0 失败，`token(prompt=14661 completion=9850 total=24511)`；日志三段齐全（管道开始 / 抓取阶段结束 / 管道结束）；`cli fetch` 的表格与汇总抽成 `_print_fetch_outcomes` 复用，`cli score` 的汇总抽成 `_print_score_result`，两处输出未变 |
| [x] M3-2 | repository 补齐：`query_digest(day_start_utc, day_end_utc, module, limit)`，排序为 `(score IS NULL) ASC, score DESC, published_at DESC, id DESC`；`mark_clicked()` 用 `COALESCE` 保留首次点击时间 | `app/repository.py`、`app/models.py` | M3-1 | plan §13.1 用例 8/10 通过（`tests/test_digest.py`）：排序断言 `[高分新(88), 高分旧(88), 低分(42), 未评分]`、同分同时间退回 `id DESC`、module 筛选与 limit 生效；`mark_clicked` 两次调用后 `clicked_at` 仍为旧值；条目不存在时 `mark_clicked` 返回 False、`get_item_url` 返回 None |
| [x] M3-3 | 候选池定义落死：`day_start_utc <= fetched_at < day_end_utc`，边界按 `config.app.timezone`（Asia/Shanghai）折算 UTC；当日为 0 条时回退 `lookback_hours=48` 并在页面顶部标注「非今日数据」 | `app/repository.py`、`app/routers/digest.py` | M3-2 | plan §13.1 用例 9 通过：上海 09-22 00:00:00（UTC 09-21T16:00）与 23:59:59（UTC 09-22T15:59:59）同属 09-22 日报，次日 00:00:00（UTC 09-22T16:00）落到 09-23 日报；`local_day_bounds` 直接断言窗口值；真实回退场景：09-22 抓的 112 条在 09-23 打开首页时已落在当日窗口外，正是靠 48 小时回退才看得见（`tests/test_digest.py::test_home_page_falls_back_to_lookback_window_with_notice` 覆盖标注文案） |
| [x] M3-4 | 路由 `/`（全模块混合，取 `digest.limit` 条）与 `/go/{item_id}`（`mark_clicked` → 302 外链；`item_id` 不存在返回 404） | `app/routers/digest.py`、`app/main.py` | M3-3 | 真实服务器：`curl /` → HTTP 200；`/go/356` 两次均 302，`Location: https://juejin.cn/post/7688270826491723802`，两次相隔 2 秒而 `clicked_at` 始终是 `2026-09-23T01:54:18Z`（首次未被覆盖）；`/go/999999` → 404 |
| [x] M3-5 | 模板：`base.html`（导航骨架 + 内容块）、`digest.html`、`partials/item_card.html`（标题/来源/打分/理由/时间，三处复用）；打分视觉分级用 CSS 变量（≥75 绿 / 50-74 灰 / <50 浅 / 无分显示「未评分」） | `app/templates/*`、`app/static/style.css` | M3-4 | 首页渲染 8 条（`共 8 条（上限 8）`），徽标依次 `72 / 45 / 40 / 40 / 38 / 38 / 35 / 34`（降序正确）；`/static/style.css` 返回 200 且浏览器实测生效（卡片白底 + 1px 边框 + 10px 圆角，浅灰底，无横向溢出、无文字重叠），导航「日报」在位；未评分卡片显示「未评分」而非空白分数，由 `test_home_page_renders_cards_and_records_click` 用无分条目覆盖（真实库 147 条全有分，构造不出样本） |
| [x] M3-6 | 空数据态：无候选且首轮抓取未完成时显示「首次抓取进行中」 | `app/routers/digest.py`、`digest.html` | M3-5 | 清库实跑：`DESKHUB_DB_PATH=/tmp/deskhub-empty.db uvicorn … --port 8766` → `curl /` HTTP 200 且页面显示「首次抓取进行中：服务刚起来，稍等一会儿刷新本页就能看到今日精选。」（不报错、不误导成「今天没内容」） |
| [x] M3-7 | 测试：日报排序（有分在前、降序、未评分末尾、并列按 `published_at` 降序）+ 跨日边界 | `tests/test_digest.py` | M3-6 | `.venv/bin/python -m pytest tests -q` → **87 passed**（url_hash 30 + fetcher 12 + repository 3 + scorer 21 + llm 10 + digest 11），全程无真实网络访问 |

**M3 完成判据：已满足** —— 首页 8 条精选（降序 72→34），点击 `/go/356` 302 跳原文且 `clicked_at` 有值并被保留。（spec §10 第 3 条）

M3 校准记录（M3-3/M3-5 的原始证据）—— 2026-09-23 09:54 打开首页：

| 分 | module | 标题（截断） |
|---|---|---|
| 72 | agent | AI Agent 真正干活的 Harness 到底是啥，原来就这 7 个子系统？ |
| 45 | bigdata | 制造业质量追溯01：最小测试用例与 Oracle 层次查询演示 |
| 40 | agent | 一个 jar 搞定实时推送：我的轻量级 SSE 中间件 Stream Nexus |
| 40 | agent | 你的网站准备好被AI Agent阅读了吗？2026年最被忽视的前端工程问题 |
| 38 | agent | 不受控的 Agent，凭什么上生产系统？ |
| 38 | bigdata | drain 卡了 40 分钟：PDB 才是节点维护的主语 |
| 35 | bigdata | 使用 DuckDB 分析 CSV 文件 |
| 34 | agent | AI 热点日报（2026-09-23）：SpaceXAI发布Grok 4.7…… |

排序、候选池与「非今日数据」判定都按 plan §9 工作；当天 8 条全部 < 75，再次印证 M2 记下的「分数分布偏左」——**没有为了好看抬分**。

实测补充（plan 未写明，留给后续里程碑决策）：

- **M2 留下的「同 prompt 重跑分数会漂」在 M3 不构成问题**：`cli pipeline` 只打「待打分池」（近 7 天、无 `item_scores` 记录），不重跑历史，所以同一天内首页列表不会跳。故 **`scoring.temperature` 保持 0.2，不改**，M2 提的两个对策选了「每天只对新增打分」这条。
- **base.html 的导航只放了「日报」**：plan §10 列了 7 个入口，但 `/m/{module}`、`/search`、`/sources`、`/funds`、`/coins` 在 M4/M5 才存在，先挂上去就是点了必 404。M5-5 一次性补齐其余 6 个。
- **Jinja2 环境集中在 `app/routers/__init__.py`**（一份 `templates` + `local_time` / `module_label` 两个过滤器）：M4 的 `search.html`、M5 的 `module.html` / `sources.html` 都要复用同一套渲染与时间显示，各建一份必然漂。plan §4 没列这个文件，属实现细节，不新增能力。
- **路由写成同步 `def`**：`sqlite3` 是同步库，FastAPI 会把同步处理函数丢进线程池，比在路由里到处写 `asyncio.to_thread` 更省事，也不会阻塞事件循环（plan §6.3 只约束了写操作）。
- **`mark_clicked()` 的返回值只看「条目在不在」，区分不了「是不是第一次点」**：`UPDATE … COALESCE` 的 rowcount 反映**匹配行数**而非「值是否变化」，重复点击仍返回 True；`/go/{id}` 的 404 判定因此走 `get_item_url()`，`mark_clicked()` 的布尔值只用于「条目不存在」这一种情况。
- **测试里跑 lifespan 会写真实日志**：`tests/test_digest.py` 的 `web` fixture 靠 `DESKHUB_DB_PATH` 把库切到临时目录，但 `app.log_path` 没有环境变量覆盖口，日志仍写真实的 `data/logs/deskhub.log`。换来的是「首页真的能被渲染出来」这条端到端证据；若以后嫌脏，再给 `log_path` 加一个 `DESKHUB_LOG_PATH`。
- **首次抓取仍未接回 lifespan**（plan §11.1 第 6 步）：按 plan §14 归属 M6-1，故现在起服务后首页要等 `cli pipeline` 或 M6 的启动补拉才有当日数据；在 M6 之前，空库首页会长时间停在「首次抓取进行中」。

---

## M3 修订记录

### R1（2026-09-23）：源把本地时间标成 GMT → 源级时区修正

**触发**：用户看库时发现 `fetch_runs` 里「刚抓取的怎么是 1 点」，怀疑时区问题并要求「其他表里有问题的也一起修」。

**审计结论（先证伪，再找真问题）**：

- `fetch_runs.started_at` / `sources.last_ok_at` / `items.fetched_at` / `items.clicked_at` / `item_scores.scored_at` **全部一致为 UTC**，符合 plan §6.1 的存储约定；`01:53Z` 就是北京时间 `09:53`，页面也已按 `Asia/Shanghai` 显示。**这几张表不需要改**。
- 真问题在 `items.published_at`：**InfoQ 中文的 feed 把北京时间当 GMT 标**，导致该源 24 条发布时间整体超前 8 小时。三条实测证据：
  1. 2026-09-23 10:25:06（北京）下载它的 feed，channel 级 `pubDate` 自称 `10:25:06 GMT`，而那一刻真实 UTC 是 `02:25:20` —— 自称时间比真实时间超前整整 8 小时；
  2. 09-22 抓取时（UTC `08:15:52`）它标称的最新一条是 `15:12:00 GMT` —— 比抓取时刻还晚 7 小时，物理上不可能；对照同批掘金为 `08:01:28Z`（比抓取早 14 分钟，合理）；
  3. 库里 InfoQ 的 `published_at` 与它的 pubDate 串逐字一致（`15:12:00` / `14:32:17` / `14:17:25` / `13:00:00` / `11:22:09` 全对得上），排除我方解析引入偏差。
- 影响：该源卡片显示成未来时间（那条 Cloudflare 显示「09-23 17:26 发布」）、prompt 的「时效性 20 分」虚高、并列排序（按 `published_at`）跟着偏。其余源正常（美团 `published_at` 为 NULL 是 M1 已记录的「源本身不给时间」）。

| # | 任务 | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|
| [x] R1-1 | 源配置新增可选 `published_tz` + 校验 + `Config.source_for(url)` 按 url 回配置取该字段 | `app/config.py` | — | 真实 `sources.yaml` 解析：InfoQ=`Asia/Shanghai`、掘金=`None`；非法值 `Asia/Shanghaix` 与空串都拒绝启动并指出位置（`sources.yaml.sources[0].published_tz：无效时区 'Asia/Shanghaix'`） |
| [x] R1-2 | 抓取时按该时区把 feed 时间重新解释成 UTC（`_entry_published_at` / `parse_feed` 增加该参数） | `app/fetcher.py` | R1-1 | `tests/test_fetcher.py` 新增 3 条：声明后 `09:26 GMT` → `01:26Z`（−8h）；未声明的源维持 `09:26Z`（掘金/博客园靠这条）；修正只对被声明的 url 生效，同轮其他源不受影响 |
| [x] R1-3 | 给 InfoQ 加 `published_tz: Asia/Shanghai` 并写明实测依据 | `config/sources.yaml` | R1-2 | 服务重启加载正常（`源清单已同步：6 个源`，无告警） |
| [x] R1-4 | 回填已有 24 条 `published_at` | — | R1-2 | 24 条全部 −8h：范围从 `2026-09-21T15:04Z` ~ `2026-09-23T09:26Z`（含未来时间）变为 `2026-09-21T07:04Z` ~ `2026-09-23T01:26Z`；全库 `published_at > now` 条数 **0**；真实重抓 InfoQ 新增 2 条（id 431/432）时间为 `02:05:17Z` / `02:00:00Z`，均在抓取时刻 `02:29:40Z` 之前 ✅ |
| [x] R1-5 | 按层级回写文档 | `spec.md`、`plan.md` | R1-4 | spec §2.3 补「源的时间戳也要当数据看」实测结论；plan §5.2 加字段说明、§7.1 步骤 6c 加解析说明、§12 加第 12 条差异、§13.1 加用例 |
| [x] R1-6 | 测试回归 | `tests/test_fetcher.py` | R1-2 | `.venv/bin/python -m pytest tests -q` → **90 passed**（fetcher 从 12 → 15） |

**记账（本次不处理）**：这 24 条的打分是用偏 8 小时的发布时间算出来的 —— 时效性只占 20 分且这些条目都是当日新发（本来就接近满分），故不回填打分；下次 `--rescore` 或改 prompt 时会自然修正。若以后要复核，按 M2 的实测值（112 条约 87147 tokens / 91 秒）估算成本。

---

## M4 — 历史全量搜索 — 已完成

| # | 任务 | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|
| [x] M4-1 | repository：`search_items(q, module, start, end, page, page_size)`，标题 + 摘要 LIKE，返回结果与总数 | `app/repository.py` | M3 | plan §13.1 之外的自测（`tests/test_search.py` 直调函数）：`q="LangGraph"` 命中标题、`q="Spark"` 命中摘要、摘要为 `NULL`/空串的条目仍能被标题命中；`module` + 时间窗口叠加后只留 1 条；时间窗口按**发布时间**折算、左闭右开（`fetched_at` 全相同的 5 条里只有发布时间落在窗口内的 2 条命中，见 R1）；`page=3&page_size=2` 取到剩余 1 条、`page=99` 返回空列表但总数仍为 5；`page=0&page_size=0` 自行兜底不抛错；新增 `day_bounds_utc(tz, day)` 并把 `local_day_bounds` 改为复用它（原 M3 断言不变） |
| [x] M4-2 | 路由 `/search`：关键词 + 时间范围 + module + 分页参数校验与透传 | `app/routers/search.py`、`app/main.py` | M4-1 | 真实服务器（127.0.0.1:8767，真实库）：`?page=abc` / `?page=-3` / `?page=9999&page_size=99999` / `?start=2026-13-99&end=notadate&module=unknown` **全部 HTTP 200，无 500**；`page=abc` 兜底成第 1 页（共 149 条 · 第 1 / 8 页），`module=unknown` 当作「全部」并仍列出条目；页码超出范围时落到最后一页而不是空页 |
| [x] M4-3 | 模板 `search.html`：结果复用 `partials/item_card.html`，外链走 `/go/{id}`；原生 JS 仅做「展开高级筛选」 | `app/templates/search.html`、`app/templates/base.html`、`app/static/style.css` | M4-2 | 浏览器实测：`/search?q=DuckDB` 渲染 1 张卡片（标题 / 来源「博客园首页」/ 入口标签 / `09-22 20:16 发布` / 徽章 35 / 理由文字），无横向滚动、无重叠裁切；「高级筛选」点一次展开（`aria-expanded=true`）、再点收起；`?page_size=5&module=bigdata&start=2026-09-22` 高级区**默认展开**且日期=2026-09-22、入口=大数据（R1 后复核：共 38 条 · 第 1 / 8 页，字段标签为「发布起始日期」），分页条为「虚线不可点的上一页 / 第 1 / N 页 / 下一页」；点「下一页」后 URL 变成 `?module=bigdata&start=2026-09-22&page=2&page_size=5`（**筛选条件随链接一起走，可分享、可回退**）且卡片内容与第一页不同 |
| [x] M4-4 | 验收：搜索今日抓到的条目标题关键词能命中 | — | M4-3 | 真实库 149 条：`/search?q=Open+Code+Review` 命中今日抓到的「阿里 Open Code Review 登顶 GitHub Trending 周榜第一」（id 431）；`?q=DuckDB` 命中 1 条、叠加 `module=agent` 为 0 条 / `module=bigdata` 为 1 条；时间范围按发布时间筛（R1 后）：`?start=2026-09-23&end=2026-09-23` 共 11 条、`?start=2026-09-22&end=2026-09-22` 共 88 条、不设时间 149 条 —— 均与 `sqlite3` 直接计数一致 —— **spec §10 第 4 条满足** |

**M4 完成判据：已满足** —— `/search` 能按关键词 + 时间范围 + 入口搜到历史条目并分页，链接可分享可回退，坏参数一律降级不报错。测试：`.venv/bin/python -m pytest tests -q` → **98 passed**（新增 `tests/test_search.py` 8 条，全程无真实网络访问）。

实测补充（plan 未写明，留给后续里程碑决策）：

- **搜索的时间范围与排序都按 `published_at`**（plan 只写了函数签名，已回写为 plan §12 第 13 条）：排序不套日报的「分数优先」，否则重打分会让翻页顺序跳。首版按 `fetched_at` 筛，因口径与页面显示不一致被推翻，见下方 M4 修订记录 R1。
- **空关键词 = 浏览全部历史**（149 条 / 8 页）：筛选条件全是可选的，没为「全量浏览」额外写代码。若不需要这个用法，把入口藏起来即可，不必改查询。
- **关键词里的 `%` / `_` 会被 LIKE 当通配符**（未做转义）：个人本地搜索，把它当「行为差异」而不是缺陷，不为此加 escape 逻辑。
- **页码越界落到最后一页**：兜底在路由层，repository 只保证「不抛错、返回空列表」；这样手改 URL 不会卡在一个空结果上。
- **无发布时间的条目在设了时间范围时搜不到**（不设时间范围照常可见）—— 这是 R1 选择「按发布时间筛」的机制性代价。库里原本的 10 条（美团）已由 **M1 修订记录 R1** 从 URL 反解补上，当前全库 `published_at IS NULL` = 0 条，代价暂时不落在任何真实条目上。
- **导航栏仍未加「历史搜索」入口**（plan §10 列了 7 个，M5-5 一次性补齐）：现在只能手输 `/search` 访问，这是 M5 的收尾项，不算 M4 缺口。
- `base.html` 新增 `{% block scripts %}` 给搜索页挂 JS：本页唯一的 JS 是「展开高级筛选」，另一件（点击外链置灰）M3 已在 base 里就位。

---

## M4 修订记录

### R1（2026-09-23）：搜索的时间范围改按「发布时间」筛

**触发**：用户筛「今天（09-23）」却看到 09-22 发布的文章，问「怎么把 22 号发布的也筛选出来了」。

**核实（先证伪，再定改法）**：不是 bug，是**口径不一致** —— 首版筛 `fetched_at`、卡片显示 `published_at`，两者不同。证据：09-23 窗口（北京时间）内抓到的 37 条里，有 26 条的 `published_at` 是 09-22，例如 id 370「JVM 线上排查实战(三)」发布 09-22 18:52、抓取 09-23 09:53 —— 它们确实是「今天抓到、昨天发布」。筛选逻辑按设计执行，但页面上看不出这个口径差异，**是会误导的设计缺陷**。是否改、改成哪种口径由用户拍板，用户选择「按发布时间筛」。

| # | 任务 | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|
| [x] R1-1 | 时间范围与排序都改按 `published_at` | `app/repository.py` | M4-1 | 真实库：`?start=2026-09-23&end=2026-09-23` **37 → 11 条**，与 `select count(*) ... where published_at >= '2026-09-22T16:00:00Z' and published_at < '2026-09-23T16:00:00Z'` 的 11 一致；页面卡片显示 `09-23 10:05 / 10:00 / 09:41 发布`；`?start=2026-09-22&end=2026-09-22` 88 条、不设时间 149 条（与全库计数一致） |
| [x] R1-2 | 页面标签写明口径：「发布起始日期 / 发布结束日期」 | `app/templates/search.html` | R1-1 | `curl "…/search?start=2026-09-22"` 页面含「发布起始日期」；标签改了但页面结构未动，M4-3 的浏览器结论（展开/收起、分页可点、无溢出）继续成立 |
| [x] R1-3 | 测试改口径，并补「不再看抓取时间」的反向断言 | `tests/test_search.py` | R1-1 | 5 条条目的 `fetched_at` 全部相同、只有 `published_at` 不同 → 命中数只随发布时间变；排序用例把发布时间顺序与 id 顺序故意错开，证明按发布时间排而非入库顺序；新增路由级用例 `test_search_page_filters_by_published_date`（两条都是今天抓的，只有发布时间不同）；`.venv/bin/python -m pytest tests -q` → **98 passed** |
| [x] R1-4 | 按层级回写文档 | `plan.md`、`tasks.md`、`README.md` | R1-3 | plan §12 第 13 条改写（含代价，以及日报为何仍用 `fetched_at`）；M4-1 / M4-3 / M4-4 的证据与附录 A 第 4 条同步更新；README 搜索说明补「时间范围按发布时间筛」 |

**代价记账**：`published_at` 为 NULL 的条目在设了时间范围时搜不到，不设时间范围照常可见。备选方案「NULL 用 `fetched_at` 兜底」会让同一列混用两种时间语义，比明着漏更难解释，故不采用。R1 当时记的「美团 10 条」这个代价已由 **M1 修订记录 R1** 消除（feed 没时间就从 URL 反解），当前全库 0 条；机制仍在，只有「既不报时间、URL 里也没有日期」的源才会落到这种情况。

**同时确认未变**：日报候选池仍按 `fetched_at`（plan §12 第 7 条），本次只动搜索，`query_digest` 一行未改。

---

## M5 — 外壳（最后做）

| # | 任务 | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|
| M5-1 | 入口视图 `/m/{module}`（`agent` / `bigdata`），复用同一候选池与卡片 | `app/routers/digest.py`、`app/templates/module.html` | M4 | 两个入口各自只出现本 module 条目；非法 module 返回 404 |
| M5-2 | 源管理 `/sources`：列表（module / 启停 / 最近成功 / 失败次数），`fail_count >= 3` 标红 + tooltip 显示最后 `error` 与 `http_status` | `app/routers/sources.py`、`app/templates/sources.html` | M4 | 手工把某源 url 改成不存在域名 → 连跑 3 次 → 页面标红 |
| M5-3 | 源操作：`POST /sources/{id}/toggle`（启停取反）、`POST /sources/{id}/fetch`（单源立即重试），均 302 回 `/sources` | `app/routers/sources.py` | M5-2 | 挂掉的源可单独重试，成功 `fail_count` 归零、红点消失 |
| M5-4 | 占位页 `/funds`、`/coins`：仅显示「未启用」，**不写任何抓取逻辑** | `app/routers/placeholders.py`、`app/templates/placeholder.html` | M5-1 | 页面只有状态说明，无数据源代码 |
| M5-5 | 导航栏（日报 / Agent / 大数据 / 历史搜索 / 源管理 / 基金 / 铜币）+ `style.css` 视觉统一 | `app/templates/base.html`、`app/static/style.css` | M5-4 | 手工点完所有导航无 404 |
| M5-6 | 验收：源集体失效时其他源不受影响，页面标红提示准确 | — | M5-5 | spec §10 第 5 条 |

---

## M6 — 收口

| # | 任务 | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|
| M6-1 | 调度器：`startup_fetch`（启动后立即、一次性）+ `daily_pipeline`（每天 08:00 Asia/Shanghai，`coalesce=True`、`misfire_grace_time=3600`、`max_instances=1`）；接回 lifespan，启动补拉用 `create_task` 不 await 不阻塞首页 | `app/scheduler.py`、`app/main.py` | M5 | 改系统时间/触发点验证 misfire 仍执行；服务启动后首轮抓取不阻塞 `/` |
| M6-2 | CLI：`purge --before YYYY-MM-DD [--yes]`（删 `items` 级联删 `item_scores`，打印删除条数） | `app/cli.py`、`app/repository.py` | M5 | 全量保留是默认策略，purge 只手工触发 |
| M6-3 | 日志轮转确认：`RotatingFileHandler` 大小/备份数生效，抓取与打分关键字段（源、status、items_new、token）可追溯 | `app/logging_setup.py` | M6-1 | 人为触发一次轮转 |
| M6-4 | 测试补齐：`tests/test_repository.py`（临时 db 跑 SQL），对齐 plan §13.1 全部 10 条用例 | `tests/` | M6-3 | `pytest` 全绿 |
| M6-5 | 终验：逐条对照 spec §10 六条验收标准 + plan §13.2 六条手工验收，记录结论 | — | M6-4 | 尤其第 6 条「连续多天我主动打开它」需**真实观察多日**才可判定 |

---

## 附录 A：验收标准对照表

| spec §10 | 对应任务 | 状态 |
|---|---|---|
| 1. 3-5 个真实源能抓到条目并入库 | M1-6、M1-4 | [x] 6 个源 ok、`items`=112 |
| 2. 新条目能被 LLM 打分，理由具体可反驳 | M2-7、M2-8 | [x] 112 条全部打上分（0 失败），随机 10 条 reason 全部可反驳 |
| 3. 首页 5-10 条精选，点击跳转且被记录 | M3-4、M3-5、M3-7 | [x] 首页 8 条（72→34 降序），`/go/356` 302 跳原文且 `clicked_at=2026-09-23T01:54:18Z` 点两次仍保留首次 |
| 4. 历史搜索能按关键词搜到过去条目 | M4-4、M4-R1 | [x] `/search?q=Open+Code+Review` 命中今日抓到的条目（id 431）；`q=DuckDB&module=bigdata` 命中 1 条，`module=agent` 0 条；时间范围按发布时间筛：`2026-09-23` 共 11 条、`2026-09-22` 共 88 条，均与 `sqlite3` 计数一致 |
| 5. 单源挂掉页面标红且不影响其他源 | M5-2、M5-6 | [ ] |
| 6. 连续多天我主动打开它 | M6-5（多日观察） | [ ] |

## 附录 B：开工前待拍板（plan §17）

| 项 | 默认值 | 影响任务 | 已定 |
|---|---|---|---|
| 日报条数 | 8（区间 5-10） | M3-5 | [10 ] |
| 打分并发与重试 | 并发 3 / 重试 1 次 | M2-5 | [ 并发 3 / 重试 1 次 ] —— 已按此跑完 112 条，峰值并发实测为 3 |
| 服务端口 | 8765 | M0-7 | [ 8765] |

## 附录 C：风险触发信号（plan §16，实现期随手对照）

| 信号 | 触发任务 |
|---|---|
| 连续 3 天候选池 < 20 条 | M1-6（补 bigdata 源） |
| LLM 400/404 非 token 问题 | M2-1（`response_format` 降级） |
| 日志 token 累计超预期 | M2-5、M2-6（不重跑历史、摘要截断） |
| 人工读 reason 无法反驳 | M2-7（改 prompt 并递增版本） |
| `database is locked` | M0-5、M6-4（WAL + busy_timeout + `to_thread`） |

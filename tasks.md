# AI 个人工作台 — tasks.md

> 依据：[spec.md](./spec.md) v1 + [plan.md](./plan.md)。
> 本文件只做**任务拆解与排序**，不新增 spec/plan 之外的能力。
> 里程碑顺序严格照 spec §13 / plan §14：**先内容质量，后外壳**。顺序搞反会得到一个漂亮的空壳。

## 0. 使用说明

- 任务按 `M0 → M6` 顺序执行，同一里程碑内按编号执行；**依赖列**标了前置任务，未满足不得开工。
- 每完成一项，勾选 `[x]` 并在「验证」列留下证据（命令输出 / 截图 / 人工结论）。
- 验证失败不进入下一个里程碑；测试用例编号对应 plan §13.1。
- 涉及外部不可控因素（源可达性、LLM 厂商兼容性）的任务，**必须实测**，不得凭猜测打勾。
- **提交粒度与推送时机**：以「一个能说清为什么的完整改动」为单位提交（不做无意义的小碎步，也不把多件事混在一笔），代码与对应文档同一次提交；**每天结束或功能完成时** `git push origin main`（远端 `git@github.com:mantianstar/deskhub.git`）。commit message 用中文说清「为什么」，而不是罗列改了哪几行。

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

- **美团技术团队的 feed 没有单条发布时间**：`<item>` 里只有 `title/link/description/content:encoded`，没有 `pubDate`/`dc:date`，故 `published_at` 存 `NULL`。影响是它在日报排序里落到当日末尾，且 M2 的 prompt 会拿到「发布时间未知」。源本身可用（200 / 10 条、内容质量高），暂不替换；若 M2 校准发现时效性分数失真，再考虑换源或从 URL 的 `/2026/09/22/` 反解日期。
- **`--dry-run` 仍会按配置同步源清单**（否则拿不到 `source_id` 无法选中源）；它保证的是不写 `items` / `fetch_runs` / 源状态，不是「完全不碰库」。
- **未预期异常按 `parse_error` 落库并打完整堆栈**：`plan §1` 铁律 2（单源失败不影响整体）与铁律 4（不吞异常）在此处冲突，处理方式是「隔离 + 落库 + `logger.exception`」，不新增第 6 种 status。数据库写失败在一源处理链的 try 外，仍会向上抛出，不会被误判成源故障。
- **`empty` 不等于故障也不洗白历史**：源返回合法 feed 但 0 条时更新 `last_ok_at` 但保留 `fail_count`，避免「长期无更新的源被标红」与「一次空响应清掉连败记录」两个方向的误判。

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

## M3 — 日报首页

目标：首页能看到当日 5-10 条精选，点击可跳转且被记录。

| # | 任务 | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|
| M3-1 | pipeline 编排：`fetch → sleep(1)（仅日志可读）→ score`，供调度器与 CLI 共用 | `app/pipeline.py` | M2 | `cli pipeline` 一次跑通 |
| M3-2 | repository 补齐：`query_digest(day_start_utc, day_end_utc, module, limit)`，排序为 `(score IS NULL) ASC, score DESC, published_at DESC, id DESC`；`mark_clicked()` 用 `COALESCE` 保留首次点击时间 | `app/repository.py` | M3-1 | plan §13.1 用例 8/10 通过 |
| M3-3 | 候选池定义落死：`day_start_utc <= fetched_at < day_end_utc`，边界按 `config.app.timezone`（Asia/Shanghai）折算 UTC；当日为 0 条时回退 `lookback_hours=48` 并在页面顶部标注「非今日数据」 | `app/repository.py`、`app/routers/digest.py` | M3-2 | plan §13.1 用例 9：上海 00:00:00 与 23:59:59 抓到的条目分属不同日报 |
| M3-4 | 路由 `/`（全模块混合，取 `digest.limit` 条）与 `/go/{item_id}`（`mark_clicked` → 302 外链；`item_id` 不存在返回 404） | `app/routers/digest.py` | M3-3 | 点两次同一链接，`clicked_at` 保持首次 |
| M3-5 | 模板：`base.html`（导航骨架 + 内容块）、`digest.html`、`partials/item_card.html`（标题/来源/打分/理由/时间，三处复用）；打分视觉分级用 CSS 变量（≥75 绿 / 50-74 灰 / <50 浅 / 无分显示「未评分」） | `app/templates/*`、`app/static/style.css` | M3-4 | 首页渲染 5-10 条，未评分卡片不显示空白分数 |
| M3-6 | 空数据态：无候选且首轮抓取未完成时显示「首次抓取进行中」 | `app/routers/digest.py`、`digest.html` | M3-5 | 清库后打开首页不报错 |
| M3-7 | 测试：日报排序（有分在前、降序、未评分末尾、并列按 `published_at` 降序）+ 跨日边界 | `tests/` | M3-6 | `pytest` 相关用例全绿 |

**M3 完成判据**：打开首页能看到 5-10 条精选，点击能跳转且 `clicked_at` 有值。（spec §10 第 3 条）

---

## M4 — 历史全量搜索

| # | 任务 | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|
| M4-1 | repository：`search_items(q, module, start, end, page, page_size)`，标题 + 摘要 LIKE，返回结果与总数 | `app/repository.py` | M3 | 直接调函数能命中当日抓到的条目 |
| M4-2 | 路由 `/search`：关键词 + 时间范围 + module + 分页参数校验与透传 | `app/routers/search.py` | M4-1 | 非法页码不 500 |
| M4-3 | 模板 `search.html`：结果复用 `partials/item_card.html`，外链走 `/go/{id}`；原生 JS 仅做「展开高级筛选」 | `app/templates/search.html` | M4-2 | 筛选条件变化后 URL 可分享、可回退 |
| M4-4 | 验收：搜索今日抓到的条目标题关键词能命中 | — | M4-3 | spec §10 第 4 条 |

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
| 3. 首页 5-10 条精选，点击跳转且被记录 | M3-5、M3-7 | [ ] |
| 4. 历史搜索能按关键词搜到过去条目 | M4-4 | [ ] |
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

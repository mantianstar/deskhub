# 铜币拍卖 — tasks

> 模块任务与验证证据。目标与验收见 [spec.md §14](../spec.md)，实现方案见 [plan.md §18](../plan.md)。
> 通用执行规则（颗粒度 / 提交约定 / 全局禁止项）见 [tasks.md §0](../tasks.md) 与 [RULES.md](../RULES.md)。
> 顺序（spec §14.10）：**先画像 → 再打通采集 → 再匹配定价 → 最后两个页面**。
> 读取边界：每块开工时**一次性读齐**必读 + 预期波及，不逐任务读（RULES.md §一）。

## 里程碑（M7 ~ M10）

### M7 — 画像（零风险，先做）

| # | 块 | 读（必读） | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|---|
| [x] M7-A | 数据层 + 解析 + 聚合（一次成文）：`coin_*` 七表 DDL + `schema_version` 1→2 + repository 读写 + `orders.xlsx` 解析 + LLM 抽字段 + 按维度聚合权重 | plan §18.4/§18.7、spec §14.2、`app/models.py`、`app/db.py`、`app/repository.py`、`app/llm.py`、`data/orders.xlsx` | `app/models.py`、`app/db.py`、`app/repository.py`、`app/coins/__init__.py`、`app/coins/profile.py` | M6 | 真实 `orders.xlsx` 1567 条全解析（0 失败）→ 979 个标签；重复导入不增行 |
| [x] M7-B | 页面 + CLI：`/coins/profile` 可编辑（启停 / 改权重 / 增删）+ `coins-parse-orders` / `coins-tags` | spec §14.2、plan §18.8/§18.9、`app/templates/base.html`、`app/cli.py` | `app/routers/coins.py`、`app/templates/coins/profile.html`、`app/cli.py` | M7-A | 浏览器实测四类操作都落库；`coins-tags` 打印「维度 / 值 / 权重」 |
| [x] M7-C | 测试 + 人工校对：`tests/test_coins_profile.py`；抽读标签、关掉明显错抽 | spec §14.9、plan §18.11 | `tests/test_coins_profile.py` | M7-B | `pytest` 全绿；抽读校对结论见实测记录 |

### M8 — 采集（最大不确定性，单独成块）

> **先单独一轮抓真实样本**（`coins-dump` 存整页 HTML），之后解析开发**全离线**（用真实 HTML 切片做 fixture），
> 不在真实网络上反复调试（RULES.md「先干跑再真跑」）。若 CDP 会话也被拦截，就地降级人工导出 HTML，不硬试。

| # | 块 | 读（必读） | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|---|
| [x] M8-A | 会话 + 采集（离线 fixture 驱动）：Playwright 持久化会话 + CDP 连本机 Chrome + `blocked` 判定 + 专场 / 详情 / 成交解析 + `coins-dump` + 采集编排 `pipeline.py` | spec §14.3、plan §18.3/§18.5 | `app/coins/mxiqi.py`、`app/coins/pipeline.py`、`app/cli.py`、`tests/fixtures/mxiqi-*.html` | M7 | 真实 HTML 切片（非手工构造）驱动全部解析用例；`pytest` 全绿 |
| [x] M8-B | 真实端到端验证（一次）：`coins-pipeline --cdp …` 抓当日专场 / 拍品并落库 | plan §18.3/§18.9 | — | M8-A | 真实 3 专场 / 133 拍品落库；解析器按真实 DOM 校准（57 件 = 页面「共57件」） |

### M9 — 推荐与页面（离线可测）

| # | 块 | 读（必读） | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|---|
| [x] M9-A | 匹配 + 定价（离线）：`matcher.py`（修饰词归一 + 命中权重和 ≥ 阈值）、`pricing.py`（关键词构造 + 中位数 / P25–P75 / 样本数 → `coin_price_refs`） | spec §14.4/§14.6、plan §18.6/§18.7 | `app/coins/matcher.py`、`app/coins/pricing.py`、`app/repository.py` | M8 | 单测覆盖归一化、阈值边界、样本 <3 只给区间、0 样本不报价 |
| [x] M9-B | 两个页面：`/coins`（仅列命中场 + 空态 + 会话失效提示）、`/coins/session/{id}`（店铺头 + 推荐拍品卡） | spec §14.7、plan §18.8、`data/shop.png`、`data/item.png` | `app/routers/coins.py`、`app/templates/coins/sessions.html`、`app/templates/coins/session.html` | M9-A | 真实数据渲染两页，字段对齐 `shop.png` / `item.png` |

### M10 — 收口

| # | 块 | 读（必读） | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|---|
| [x] M10-A | 调度 + 导航：`startup_coins` + `daily_coins`（08:00 独立 job，失败不传播）；`/coins` 由正式模块接管、`/funds` 仍占位 | spec §14.8、plan §18.9、`app/scheduler.py`、`app/main.py` | `app/scheduler.py`、`app/main.py`、`app/templates/base.html`、`app/routers/placeholders.py` | M9 | 启动日志含铜币补拉；与 `daily_pipeline` 互不阻塞 |
| [x] M10-B | 文档回写 + 端到端收口 | spec.md / plan.md / tasks.md / README.md | 同左 | M10-A | spec↔plan↔tasks 无静默跑偏；`pytest` 全绿 |

### 未结项（需要时间 / 需要你）

| # | 事项 | 依赖 | 说明 |
|---|---|---|---|
| [ ] | 多日观察（spec §14.12 第 6 条） | 时间 | 不代填 |
| [ ] | 匹配阈值 1.5 校准（spec §14.11） | 真实推荐结果 | 当前偏松，会出现只靠年代命中的弱推荐 |
| [ ] | 定时任务的登录态确认 | 你保持 9222 Chrome 常开 | `daily_coins` 走 `config.yaml` 的 `cdp_url`；会话失效会记 `blocked` |

### v1.1 实测记录（2026-10-12）

#### M7 — 画像（真实数据跑通，已满足）

- `cli coins-parse-orders`：读入 **1567** 条订单，**解析成功 1567、失败 0**，写入画像标签 **979** 个；
  `token(prompt=56932 completion=512192 total=569124)`；`coin_orders` 1567 行。
- 抽取质量（真实输出抽样）：`Lot.90 康德四年一分` → era=康德四年 / denomination=一分 / category=铜币（country 正确留空）；
  `英国1919年半便士` → country=英国 / era=1919年 / denomination=半便士。
- 画像分布（`group by dimension`）：era 457 / denomination 218 / country 182 / condition 64 / shop 45 / category 9 / price_band 4。
  高频词与现实一致：国家 **英国 297**、澳大利亚 119、新西兰 111、香港 71、日本 59；面值 **1便士 219**、半便士 171、一钱 115；
  价位 6元以下 452 / 7–11元 329 / 12–28元 394 / 29元以上 388。**1538/1567 条至少抽到一个字段（98%）**。
- **踩坑并修好**：`llm` 单次请求 30s 超时会把大多数抽取批打成 `ReadTimeout`（79 批里连续多批被跳过）。
  对策：新增 `llm.timeout_seconds`（默认 120）、`coins.profile_llm.batch_size` 降到 20、新增 `concurrency: 6`、
  抽取改并发 + `gather` 后串行落库。改完 **79 批全部成功、0 失败**。这条是实测出来的，不是预估。

#### M8 — 采集（**真实站点已打通，解析器已按真实 DOM 校准**）

- 依赖：`pip install playwright && playwright install chromium`（94.3 MiB，落在 `~/Library/Caches/ms-playwright/`）。
- **实测：Playwright 自带 Chromium 过不了滑块。** 两次尝试都被判「验证失败」（headless 与 `--headful` 一样），
  说明阿里云滑块识别了自动化浏览器。**改用 CDP 连用户自己的 Chrome**（`--cdp http://127.0.0.1:9222`）后
  **一次通过**（人工滑动 9 秒），拿到 2 个专场 / 495 条拍品。→ spec §14.3 的「会话复用」落地为 `cdp_url`。
- **踩坑并修好（两处）**：
  1. 第一版拿 `captchaV2` / `custom-slider` 当拦截特征 —— 实测它们在**每个**页面里都有（防爬 SDK 全站加载），
     会把正常页也判成被拦。改为「验证弹窗自己的痕迹」（`<title>Verification</title>` / SDK 配置串）
     + 「页面本该有的内容缺失」（`expect`）。拦截样本留作 `tests/fixtures/mxiqi-blocked.html`。
  2. **domcontentloaded 时详情页只有拍品目录**，起价 / 当前价 / 认证徽标都是之后由 JS 填的 →
     新增 `_settle()`（尽力等 networkidle + 固定停 3 秒）后再取 DOM，否则这些字段全是 None。
- **第一版解析器对着真实 DOM 全错**（把 JS 片段当专场名、把 `Lot.N` 重复切成多条、价格全 None）——
  这正是不该拿「手工构造的 fixture」当站点兼容性证明的原因。已**按真实 DOM 重写**：
  列表页按 `<div class="auction">` 切卡 + 读 `title=` 属性；详情页按 `<li id="item{拍品id}">` 切块；
  图片按资源桶区分（`/AUC/` 拍品图、`/USR/` 头像）。
- **fixture 已换成真实 HTML 切片**（`data/mxiqi-inbox/` 存了整页样本，fixture 是从中截的，未手工改写）：
  `parse_sessions` / `parse_session_detail` / `parse_sold_items` 都对它们断言真实值
  （如专场 57 件拍品、`共57件`、`押 ¥10`、`Lot.1 id=6871280`）。
- **详情页拿不到的字段用列表页兜底**：围观数在详情页是 `<span id="pv">`（JS 另发请求填）；
  封面图只有列表页有 → `parse_session_detail(..., fallback=card)`。

#### M9 — 匹配 / 定价 / 页面

- 匹配：`tests/test_coins_match.py` 11 条（归一化、禁用标签、不可匹配维度、关键词优先级、`rematch_all` 阈值）。
  **踩坑并修好**：`极美品` 会被更短的 `美品` 先咬掉、只剩一个「极」→ 改为按词长降序替换。
- 定价：`tests/test_coins_price.py` 12 条（中位数/分位、`mean`、样本 <3 只给区间、0 样本不给价、时间窗口、关键词为空不发请求）。
- 页面：`tests/test_coins_pages.py` 12 条：覆盖「无命中专场不展示」、拍品页字段齐全、≤3 图、
  「样本不足 / 无成交样本」两种标注、`blocked` 顶栏提示、「成交价要登录」提示、画像页增删改启停。
- **真实数据跑通（`coins-pipeline --cdp --limit 3`）**：3 个专场 / **133 条拍品** / 命中 5 条；
  落库后 `/coins` 与 `/coins/session/{id}` 渲染出真店铺名、认证徽标、押金 ¥100、共28件、推荐2件、5823人次围观、起价/当前价。
  自动构造的搜索关键词也合理（`巴拿马 1967` / `日本 1964 1000円`）。
- **⚠️ 推荐价被登录挡住（且踩了两个坑）**：
  1. 成交价要登录：未登录时页面写「登录后查看成交价」，接口返回同一句。
     按 spec §14.3：**人工在真实浏览器里登录一次、复用会话，程序不碰密码**。
  2. **自己发那个价格 POST 会被 WAF 拦**：用 `fetch` 和用页面自带 jQuery 都试过，
     返回 HTTP 200 但内容是阿里云 WAF 页面；而**页面自己发的请求是通的**。
     → 最终实现**只读渲染后的 DOM**（`.sold-price[item_id]` 节点），不自己发请求。
  3. **登录要发生在同一个浏览器 profile 里**：调试实例用的是独立 profile
     （`--user-data-dir=$HOME/.chrome-mxiqi`），在平时的 Chrome 里登录**不生效**。
     README 已写清两种启动方式与该注意事项。
- **查价限速（用户 2026-10-12 明确要求：不要求效率，防封号）**：新增
  `coins.price.query_interval_seconds`（默认 15，实际 ±40% 抖动）与 `max_queries_per_run`（默认 20），
  超出上限的**自动推迟到下一轮**（`CoinsRunResult.lots_pricing_deferred`，CLI 会打印、页面/日志可查）；
  配合「当日已查过的不再查」，一天最多几十次请求。已加用例：上限生效 + 推迟的那条下一轮补上。
- 页面提示：`coin_fetch_runs` 里**最近一次查价**含「登录」时报错 → `/coins` 顶部红字提示去登录
  （登录成功后该提示自动消失，不翻旧账）。
- **首次真实查价后的质量检查（2026-10-12，用户点开滑块后）**：5 条推荐里发现两类错价，都已修：
  1. **子串咬进更长数字**：画像里的面值标签「5分」命中了「**2**5分」→「美国1962年精制25分」
     被按「美国 1962 5分」查价（推荐价 ¥98）。修：数字边界保护 → 关键词变「美国 1962 25分」，
     推荐价变 **¥38.5（22 条样本）**，更合理。
  2. **关键词太泛**：`1961年精制华盛顿25美分`（关键词只剩「1961」）与
     `巴拿马1967年精制套币`（**当前价 ¥330**，关键词「巴拿马 1967」）分别被报成 ¥2。
     修：**报价前必须命中「面值/品类」**，否则主动不报价（页面标「未报价」）——
     这正是 spec §14.6 说的「宁可空着，也不给不可验证的数字」。
- **修完的最终结果**：5 条推荐 → **3 条给出推荐价**（芬兰1916芬尼 ¥30 / 日本1964 1000円 ¥125.5 /
  美国1962 25分 ¥38.5，都带区间与样本数），2 条因关键词太泛**主动不报价**。
- **待校准**：匹配阈值 1.5 仍偏松（会出现只靠泛词命中的推荐）；等积累几天真实推荐结果后再调（spec §14.11）。

#### M10 — 收口

- 调度：`startup_coins`（启动补拉，`create_task` 不阻塞）+ `daily_coins`（每天 08:00，独立 job，
  `coalesce=True` / `misfire_grace_time=3600` / `max_instances=1`）；`tests/conftest.py::no_scheduler`
  已一并挡住铜币补拉，避免测试去开浏览器。
- 导航：`placeholders.py` 只留 `/funds`；`/coins` 由正式模块接管（`tests/test_sources.py` 里原
  「铜币占位页」用例改成断言它是真模块）。
- 测试：`.venv/bin/python -m pytest tests -q` → **179 passed**（M0~M6 的 119 + 铜币 60）。
- **未完成**：多日观察未起算。

#### 下一步（需要你操作：登录一次）

```bash
# 1) 用独立 profile 启动 Chrome 并开调试端口（不动你正在用的 Chrome 窗口）
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-mxiqi"

# 2) 在弹出的 Chrome 里手动过滑块 + 登录麦稀奇（成交价要登录才看得到；程序不碰密码）

# 3) 跑整轮：采集 → 匹配 → 定价（关键词正确套用推荐价后，页面就有推荐价了）
.venv/bin/python -m app.cli coins-pipeline --cdp http://127.0.0.1:9222 --limit 20
```

> 站点改版时用 `coins-dump --cdp … --path /auction --target sessions` 存真实 HTML 再校正解析器。


---

## 附录 D：铜币拍卖验收对照（spec §14.12，v1.1 增补）

| spec §14.12 | 对应块 | 状态 |
|---|---|---|
| 1. `orders.xlsx` 能解析出订单、画像可编辑 | M7-A ~ M7-C | [x] 真实 1567 条全解析（0 失败）、979 个标签；`/coins/profile` 可编辑 |
| 2. 每天能采到当日专场与拍品（会话有效时） | M8-A/B、M10-A | [x] `--cdp` 真实抓到 3 专场 / 133 拍品并落库；解析器按真实 DOM 校准 |
| 3. 画像能筛出相关专场，无命中为空态 | M9-A/B | [x] 真实数据下落库 3 场、列表页只列命中场（2 场）；空态用例覆盖 |
| 4. 推荐价带区间 + 样本数 + 关键词，无样本不编价 | M9-A | [x] 真实成交价已跑通（3 条给出中位数+区间+样本数+关键词，2 条因关键词太泛**主动不报价**）；查价有限速（15s±抖动、单轮上限 20） |
| 5. 两页字段完整且对应 `shop.png` / `item.png` | M9-B | [x] 已用真实数据渲染两页（`curl` 核过店铺名/认证/押金/拍品数/围观/起价/当前价） |
| 6. 连续多天我主动打开它 | 未结项（多日观察） | [ ] 未起算 |

> 本模块的待定参数见 [spec.md §14.11](./spec.md)；当前实现取的是建议值（匹配阈值 1.5 / 样本下限 3 / 窗口 180 天 / 单轮上限 20 / 中位数），**匹配阈值待拿到真实推荐结果后校准**。

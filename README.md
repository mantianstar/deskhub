# deskhub — AI 个人工作台

本地 Web 应用，单用户。v1 只做一件事：把 RSS/Atom 源的内容抓下来、用 LLM 打分，生成一份「今天值得看什么」的日报。

需求见 [spec.md](./spec.md)，技术方案见 [plan.md](./plan.md)，执行任务见 [tasks.md](./tasks.md)。

## 环境

- Python 3.11+（当前开发机 3.14）
- 无数据库服务依赖，存储为单文件 SQLite

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 DESKHUB_LLM_API_KEY
```

铜币拍卖要用浏览器渲染（麦稀奇是 JS SPA + 滑块），**额外装一次 chromium**（约 95MB）：

```bash
.venv/bin/python -m playwright install chromium
```

## 启动

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
```

（`uvicorn` 只装在 `.venv` 里，没 `source .venv/bin/activate` 就直接写全路径，否则会 `command not found`。）

开发期可以加 `--reload`（改 `.py` / 模板后自动重载，省掉手工重启）。**但接上调度器后别用它**：重载等于重启进程，会打断正在跑的启动补拉、也会把每日任务重排：

```bash
.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8765
```

想让它不占着终端（关掉终端也继续跑）：

```bash
nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 > data/logs/uvicorn.out 2>&1 &
```

启动时依次执行：加载日志 → 校验配置 → 初始化数据库 → 同步源清单 → 启动调度器 → 后台跑一轮补拉。
配置校验失败会直接退出并打印原因。

补拉是后台任务，**不阻塞首页**：服务一起来页面就能打开，抓取与打分在后台继续（日志里能看到「启动补拉开始 / 结束」）。
调度器每天 `08:00`（`config.app.timezone`）自动跑一轮同样的管道；那一刻本机没开机也不要紧，
一小时内（`misfire_grace_time=3600`）起来会补跑一次，而不是丢掉这天。

打开 <http://127.0.0.1:8765/> 就是当日日报：`digest.limit`（默认 8）条，按 score 降序，
未打分的排在末尾；点标题经 `/go/{id}` 跳原文并记下 `clicked_at`。
当日没有抓到的条目时，页面会回退到最近 `digest.lookback_hours`（默认 48）小时并标注「非今日数据」。

历史搜索在 <http://127.0.0.1:8765/search>（导航栏也有入口）：关键词搜标题 + 摘要，
支持时间范围与入口筛选、分页；筛选条件都在 URL 上，可以直接收藏或分享。
**时间范围按「发布时间」筛**（与卡片上显示的时间同一个口径，plan §12 第 13 条）；
feed 和 URL 都拿不到发布时间的条目，设了时间范围会搜不到，不设时间范围照常能搜到
（美团源原本就是这种，已按 URL 路径里的日期反解，见 plan §12 第 14 条）。

导航栏共 7 个入口：**日报 / Agent 开发 / 大数据 / 历史搜索 / 源管理 / 基金 / 铜币**。

- `/m/agent`、`/m/bigdata`：入口视图，只列该入口的今日精选（与日报同一套卡片）。
- `/sources`：源管理。每个源可「启用/停用」和「立即抓取」；连续失败 `>= 3` 次的源标红，
  鼠标悬停「失败次数」能看到最后一次的 `error` 与 `http_status`。某个源挂了只标红它自己，
  不影响其他源。**注意**：界面上的启停只在本进程存活期内有效 —— 重启会被
  `config/sources.yaml` 覆盖回来（源清单是配置的唯一真源，plan §16）。
- `/funds`：纯占位页，只显示「未启用」。
- `/coins`：**铜币拍卖**（v1.1 正式模块，见下节）。

## 铜币拍卖（v1.1）

按你的收藏画像，每天从**麦稀奇（mxiqi.com）**筛出相关专场与拍品，并给出**参考价区间**。
定义见 [spec.md §14](./spec.md)，实现见 [plan.md §18](./plan.md)。

- `/coins`：**专场列表**。只列「至少命中 1 条推荐拍品」的专场（没命中就不展示）；
  卡片含拍卖时间 / 专场名 / 店铺头像 / 4 张商品图 / 拍品数 / 围观数。
- `/coins/session/{id}`：**拍品页**。店铺头（头像 / 名称 / 认证标识 / 拍卖时间 / 专场名 / 押金 / 拍品总数 / 推荐拍品数）
  + 推荐拍品（名称 / 起拍价 / 当前价 / **推荐价 + 区间 + 样本数 + 关键词** / 最多 3 张图）。
- `/coins/profile`：**画像**。标签按维度分组，可启停 / 改权重 / 增删。

**画像**由 `data/orders.xlsx`（你的历史成交导出，1567 条）生成：LLM 从「宝贝名称」抽出
国家 / 年代 / 面值 / 品类 / 品相，再按维度统计频次得到权重。

**推荐价**的依据是麦稀奇**站内历史成交**（按关键词搜「成交」），不是你的个人成交：
主价取中位数、另给 P25–P75；**样本 < 3 条只给区间、0 条不报价**（不给不可验证的数字）。

### 怎么采集（推荐：连本机 Chrome）

麦稀奇有阿里云滑块，**Playwright 自己启动的 Chromium 会被识别、滑块过不去**
（实测连续两次报「验证失败」）。可靠做法是**连你自己已经打开的 Chrome**：

```bash
# 方式一（推荐，复用你平时的登录态）：先 Cmd+Q 完全退出 Chrome，再这样启动
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222

# 方式二（不动你正在用的窗口）：用一个独立 profile 起第二个 Chrome
#   注意：那是全新的 profile，**里面没有你的登录态**，要在那个窗口里单独登录一次
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-mxiqi"

# 采集（会在那个 Chrome 里开标签页；遇到滑块请手动滑一下，最多等 180 秒）
.venv/bin/python -m app.cli coins-pipeline --cdp http://127.0.0.1:9222 --limit 20
```

### 推荐价需要登录（一次性）

实测：**成交价要登录才看得到**（页面显示「登录后查看成交价」）。请在**上面那个 Chrome
窗口里**登录一次麦稀奇 —— 如果你用的是方式二，登录要发生在那**独立 profile 的窗口**里
（不是平时的 Chrome）；会话会留在对应 profile 里，之后所有 `--cdp` 命令自动带上登录态。

> `spec §14.3` 的约定：**程序不保存你的账号密码**；登录由你在真实浏览器里做，只复用会话。
> 没登录时页面会红字提示「成交价要登录才能看」，拍品显示「未查价」，其余功能照常。

### 查价限速（防封号）

成交价是**拿你自己账号**在查，所以默认很保守（`config.yaml` 的 `coins.price`）：

| 参数 | 默认 | 含义 |
|---|---|---|
| `query_interval_seconds` | `15` | 每次查价之间停 15 秒（实际带 ±40% 抖动 → 9~21 秒，避免固定节奏） |
| `max_queries_per_run` | `20` | 单轮最多查 20 条；**超出的自动推迟到下一轮**（CLI 会打印「推迟 N 条」） |

再加上「当日查过的当天不重复查」，正常一天最多几十次请求。要更保守就把这两个值调大/调小。

### 其它命令

```bash
.venv/bin/python -m app.cli coins-dump --cdp http://127.0.0.1:9222 \
    --path /auction --target sessions        # 存渲染后的真实 HTML（站点改版时校正解析器）
.venv/bin/python -m app.cli coins-fetch --headful --limit 3   # 退路：Playwright 自带浏览器（可能过不了滑块）
```

> 服务进程里跑的采集是 headless 的，**页面上的「刷新采集」按钮过不了滑块** ——
> 那条路只适合会话还有效时自愈。

## 停止与重启

服务是前台进程，**改了代码必须重启才生效**（没加 `--reload` 时）。

```bash
# 1) 首选：回到当初跑服务的那个终端，按 Ctrl+C

# 2) 找不到那个终端：先看端口被谁占着
lsof -nP -iTCP:8765 -sTCP:LISTEN

# 3) 按进程名停（会杀掉本机上所有 `uvicorn app.main:app` 进程）
pkill -f "uvicorn app.main:app"

# 4) 只停某一个（把 <PID> 换成上面 lsof 查到的进程号）
kill <PID>
```

一条命令完成「停 + 起」：

```bash
pkill -f "uvicorn app.main:app"; sleep 1; .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
```

重启后确认新进程活着：

```bash
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:8765/healthz   # 期望 200
```

启动时报 `Address already in use` = 旧进程没停干净，回到第 3、4 步。

重启不影响数据：库在 `data/deskhub.db`，重启只是重新建表校验 + 同步源清单，不会清 `items`；
每次启动都会自动跑一轮补拉（抓取 + 打分），不用再手工 `cli pipeline`。

## 自检

```bash
curl -s 127.0.0.1:8765/healthz | python3 -m json.tool
```

期望返回：

```json
{
  "db": "ok",
  "sources_total": 7,
  "sources_failing": 0,
  "last_fetch_at": "2026-09-22T08:18:48Z"
}
```

（`sources_total` 含已被配置停用的源；`last_fetch_at` 为最近一次抓取的 UTC 时间，这里只是示例值。）

## 配置

- `config/config.yaml`：应用、日报、抓取、打分、LLM、模块关注点
- `config/sources.yaml`：源清单（url 唯一，重复会在启动时报错退出）
- 覆盖优先级：**环境变量 > `.env` > `config.yaml`**
- `data/deskhub.db` 与 `data/logs/` 为运行时生成，已在 `.gitignore` 中

## 当前进度

- M0（骨架）：服务可启动、库表可建、`/healthz` 可用。
- M1（抓取管道）：`config/sources.yaml` 里的 6 个中文源可真实抓取入库，失败可分类（`ok` / `empty` / `http_error` / `timeout` / `parse_error`），`url_hash` 保证重复抓取不产生重复数据。
- M2（LLM 打分）：待打分条目（近 7 天、未打分）逐条调用 LLM，产出 `score` + 一句可反驳的 `reason`；单条失败重试 1 次，仍失败则留在池中下轮重试，不写库。库里未打分的条目每轮都会被补上。
- M3（日报首页）：`fetch → score` 由 `app/pipeline.py` 编排（调度器与 `cli pipeline` 共用）；首页按 `score` 降序展示当日精选，点击经 `/go/{id}` 记录 `clicked_at`（保留首次）。
- M4（历史搜索）：`/search` 支持关键词（标题 + 摘要 LIKE）+ 时间范围 + 入口筛选 + 分页，结果复用日报卡片；筛选条件都在 URL 上，可分享、可回退。
- M5（外壳）：导航栏 7 个入口全部可用；`/m/{agent,bigdata}` 入口视图、`/sources` 源管理（启停 / 单源重试 / 连败标红 + tooltip）、`/funds` 与 `/coins` 占位页；非法 `/m/xxx` 返回 404。
- M6（收口）：`app/scheduler.py` 接进 lifespan（启动补拉 + 每天 08:00 出报）、`cli purge` 手工清理旧条目、日志轮转与关键字段可追溯确认、数据层测试补齐。v1 六条验收标准的逐条结论见 `tasks.md` M6 小节。
- M7（铜币画像）：`data/orders.xlsx` 1567 条全部解析入库（0 失败）、产出 979 个画像标签（英国 297 / 1便士 219…）；`/coins/profile` 可启停 / 改权重 / 增删。
- M8（铜币采集）：Playwright 持久化会话 + `--headful` 人工过滑块 + `blocked` 检测与提示链路已跑通；**站点解析器只用手工构造的 fixture 回归过，真实 DOM 样本待 `--headful` 跑通后校正**。
- M9（铜币推荐与页面）：匹配 / 定价（中位数 + P25–P75，样本 <3 只给区间、0 样本不报价）/ 两个页面均已实现并有离线用例。
- M10（铜币收口）：`startup_coins` + `daily_coins`（08:00，与知识日报互不阻塞）、导航改正式入口、`pytest` 170 passed。
  **端到端与多日观察卡在麦稀奇滑块**：本机 headless 必被拦，需先人工过一次滑块。

## 手工命令

```bash
.venv/bin/python -m app.cli fetch              # 抓取所有 enabled 的源并入库
.venv/bin/python -m app.cli fetch --dry-run    # 只打印解析结果，不写 items / fetch_runs
.venv/bin/python -m app.cli fetch --source-id 3  # 只抓某个源（忽略启停，便于单独重试）
.venv/bin/python -m app.cli sources            # 打印源状态表格（启停 / 连败 / 最近成功）
.venv/bin/python -m app.cli pipeline           # 抓取 + 打分一次跑完（手工出报）
.venv/bin/python -m app.cli score              # 给待打分条目打分（默认上限 200 条）
.venv/bin/python -m app.cli score --limit 3    # 先小批量试一次（换厂商/换模型时用）
.venv/bin/python -m app.cli score --rescore --prompt-version v1  # 删该版本打分并整批重跑
.venv/bin/python -m app.cli purge --before 2026-09-01            # 预览会删多少条（不删任何东西）
.venv/bin/python -m app.cli purge --before 2026-09-01 --yes       # 真删：items 级联删 item_scores
```

铜币拍卖（v1.1）：

```bash
.venv/bin/python -m app.cli coins-parse-orders        # 解析 data/orders.xlsx → 画像（调 LLM，一次性）
.venv/bin/python -m app.cli coins-rebuild-profile     # 只用已入库订单重算画像标签（不调 LLM）
.venv/bin/python -m app.cli coins-tags                # 打印画像标签表格
.venv/bin/python -m app.cli coins-fetch --headful     # 采集专场+拍品；--headful 用来人工过一次滑块
.venv/bin/python -m app.cli coins-price               # 只对已入库的推荐拍品查价
.venv/bin/python -m app.cli coins-pipeline --limit 20  # 采集 → 匹配 → 定价 一整轮
.venv/bin/python -m app.cli coins-dump --path /auction --target sessions  # 存渲染后的 HTML 供校正解析器
```

`purge` 删的是 `--before` 那天 `00:00`（业务时区）之前**抓到的**条目（`fetched_at`），
`item_scores` 随外键级联删除，`sources` / `fetch_runs` 保留；不带 `--yes` 时只打印将删除的条数。
全量保留是默认策略，不做自动清理（spec §11）。

# deskhub — AI 个人工作台

本地 Web 应用，单用户。当前两个模块：

- **知识聚合**：抓 RSS/Atom → LLM 打分 → 生成「今天值得看什么」日报。
- **铜币**：按收藏画像从麦稀奇筛专场 / 拍品并给参考价；另有 Numista 参考库（目录 / 各品相估值 / 正反图）。

需求见 [spec.md](./spec.md)、技术方案见 [plan.md](./plan.md)、执行任务见 [tasks.md](./tasks.md)（三者都是索引，按模块分册）；
**改了什么、为什么**见 [CHANGELOG.md](./CHANGELOG.md)。
本文件只讲**怎么用**，实现细节不在 README 重复。

## 环境

- Python 3.11+（当前开发机 3.14）
- 无数据库服务依赖，存储为单文件 SQLite

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 DESKHUB_LLM_API_KEY（铜币参考库另需 DESKHUB_NUMISTA_API_KEY）
```

## 启动

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
```

（`uvicorn` 只装在 `.venv` 里，没 `source .venv/bin/activate` 就直接写全路径，否则会 `command not found`。）

开发期可加 `--reload`，**但接上调度器后别用**：重载等于重启进程，会把每日任务重排。

想让它不占着终端（关掉终端也继续跑）：

```bash
nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 > data/logs/uvicorn.out 2>&1 &
```

启动时依次执行：加载日志 → 校验配置 → 初始化数据库 → 同步源清单 → 启动调度器。配置校验失败会直接退出并打印原因。

**重启不再自动抓取 / 打分 / 采集**（2026-10-15 起）：本机经常重启，而一轮自动补拉就是一次真实抓取外加一次 LLM 打分。
现在只在每天 `08:00`（`config.app.timezone`）自动跑，且四个环节（知识抓取 / 打分、铜币采集 / 查价）**各自可关**。
那一刻本机没开机也不要紧：一小时内（`misfire_grace_time=3600`）起来会补跑一次。
要立刻跑一轮，去 <http://127.0.0.1:8765/control> 点「立即运行」，或手工跑下面的 CLI。

## 页面

导航栏 8 个入口：**日报 / Agent 开发 / 大数据 / 历史搜索 / 源管理 / 运行控制 / 基金 / 铜币**。

| 路由 | 用途 |
|---|---|
| `/` | 当日日报：`digest.limit`（默认 8）条按 score 降序，未打分排末尾；点标题经 `/go/{id}` 跳原文并记 `clicked_at`。当日无条目时回退到最近 `digest.lookback_hours`（默认 48）小时并标注「非今日数据」 |
| `/m/agent`、`/m/bigdata` | 入口视图：只列该入口的今日精选 |
| `/search` | 历史搜索：关键词（标题 + 摘要）+ 时间范围 + 入口筛选 + 分页；筛选条件都在 URL 上，可收藏 / 分享。**时间范围按「发布时间」筛** |
| `/sources` | 源管理：每个源可「启用 / 停用」「立即抓取」；连续失败 `>= 3` 的源标红（悬停看最后一次 `error` 与 `http_status`）。界面启停只在本进程存活期内有效，重启会被 `config/sources.yaml` 覆盖回来 |
| `/control` | 运行控制：四个功能开关（控制每天 08:00 的定时任务，存库、重启保留）+ 各自的「立即运行」（手动跑一次、忽略开关、后台执行） |
| `/funds` | 占位页，只显示「未启用」 |
| `/coins`、`/coins/profile`、`/coins/collection`、`/coins/collections`、`/coins/grades`、`/coins/info`、`/coins/manage` | 铜币各入口，见下 |

## 铜币

- **拍卖**：`/coins` 专场列表（只列至少命中 1 条推荐拍品的专场）→ `/coins/session/{id}` 拍品页（店铺头 + 推荐拍品）。
  `/coins/profile` 旧版标签画像：按维度分组，可启停 / 改权重 / 增删；
  配置收藏系列后，每日匹配改由 `/coins/collection` 驱动。推荐价依据麦稀奇**站内历史成交**
  （中位数 + P25–P75，样本 < 3 只给区间、0 条不报价）。
- **收藏画像 / 推荐**：`/coins/collections` 手工把多个 Numista series 合并成收藏系列
  （同国家 + 同执政者 + 同面值 + 尺寸差 ≤ 2mm）；`/coins/collection` 展示年份跨度、已有 / 缺失 / 需升级年份、
  年份规格字典与三层推荐（缺失年份 → 差品升级 → 跨系列相似），并匹配当日截拍专场的已入库拍品。
  采集只抓当日截拍的店铺；日本年号会翻译回 `明治6年` / `明治六年`。
- **品相录入**：`/coins/grades` 把历史订单按「国家 + 年份 + 面值」去重后，人工为每个组合拉一档品相（好 / 普通 / 差），
  点「保存」写库；国家 / 年份 / 面值按 Numista 口径统一（`英属新西兰` → `新西兰`、`1910年` → `1910`、`1便士` → `1 便士`），
  历史订单里没有的组合也可用页内「新增组合」补一条；行尾「删除」连该组合的历史订单一起删（需二次确认）。
  品相存独立表，会被收藏画像用于判定「需升级年份」（仅「差」）。
- **参考库（Numista）**：`/coins/info` 查询本地已采集系列 → `/coins/series/{numista_id}` 系列详情（系列信息 + 各年份信息表）；
  `/coins/manage` 采集管理（两步：按钮 1「获取系列」→ 结果行按钮 2/3/4「获取年份信息 / 年份估值 / 下载图片」）。

### 采集与查价（都不用浏览器）

采集（专场 + 拍品）与查价（站内历史成交）都走麦稀奇**小程序网关** `gapi.mxiqi.com`（免登录、免签名），普通 JSON POST 即可：

```bash
.venv/bin/python -m app.cli coins-fetch                 # 只采集 + 匹配
.venv/bin/python -m app.cli coins-price                 # 只对已入库的推荐拍品查价
.venv/bin/python -m app.cli coins-pipeline              # 采集 → 匹配 → 查价（一整轮）
```

> 「拍卖时间」按站内口径存 **截拍时间**（`end_time`）。

查价会对网关打一批请求，默认保守（`config.yaml` 的 `coins.price`）：

| 参数 | 默认 | 含义 |
|---|---|---|
| `query_interval_seconds` | `15` | 每次查价之间停 15 秒（实际带 ±40% 抖动 → 9~21 秒，避免固定节奏） |
| `max_queries_per_run` | `20` | 单轮最多查 20 条；超出的自动推迟到下一轮（CLI 会打印「推迟 N 条」） |

再加上「当日查过的当天不重复查」，正常一天最多几十次请求。

### 参考库采集（`coins-values` / `coins-images` 需要先开一个带调试端口的 Chrome）

```bash
# 目录信息（官方 API，需要 .env 里的 DESKHUB_NUMISTA_API_KEY，免浏览器）
.venv/bin/python -m app.cli coins-series --country 英国 --year 1900 --denomination 1便士
.venv/bin/python -m app.cli coins-issues --series-id 670

# 各品相估值（G/VG/F/VF/XF/AU/UNC）+ Frequency（走类型页，需 CDP Chrome）
.venv/bin/python -m app.cli coins-values --series-id 670

# 正 / 反图缓存到本地（同样走类型页那条 CDP 链路）
.venv/bin/python -m app.cli coins-images --series-id 670

# 读英文表重翻中文表（不重采 Numista、不耗额度；需要 LLM Key）
.venv/bin/python -m app.cli coins-translate --series-id 670
```

先起一个带调试端口的 Chrome（独立 profile，跟平时那个互不影响）：

```bash
open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-numista"
```

端口不是 9222 就用 `--cdp http://127.0.0.1:9222` 指定（网页上的按钮 3 / 4 同样要求它开着）。
估值与图片只能这样取：numista.com 挡着 Cloudflare 的 JS 校验，`curl` / `httpx` / 指纹伪装一律 403；
Numista 图片又带 `cross-origin-resource-policy: same-origin`，页面里没法外链，所以只能缓存到本地。
这两条链路**不进每日调度**，手工按需跑；不需要 `playwright install`（用的是你自己的 Chrome，只通过 CDP 挂上去）。

> 页面只用中文表（`coin_series_cn` / `coin_issues_cn`），不做中英切换；未缓存的图显示「原图链接」或占位，不出现破图。

## 停止与重启

服务是前台进程，**改了代码必须重启才生效**（没加 `--reload` 时）。

```bash
# 1) 首选：回到当初跑服务的那个终端，按 Ctrl+C

# 2) 找不到那个终端：先看端口被谁占着
lsof -nP -iTCP:8765 -sTCP:LISTEN

# 3) 按进程名停（会杀掉本机上所有 uvicorn app.main:app 进程）
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
重启不影响数据：库在 `data/deskhub.db`，重启只重新建表校验 + 同步源清单，不清 `items`。

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

- `config/config.yaml`：应用、日报、抓取、打分、LLM、模块关注点、铜币
- `config/sources.yaml`：源清单（url 唯一，重复会在启动时报错退出）
- 覆盖优先级：**环境变量 > `.env` > `config.yaml`**
- `data/deskhub.db`、`data/logs/`、`data/coin_images/` 为运行时生成，已在 `.gitignore` 中

## 手工命令

```bash
.venv/bin/python -m app.cli fetch                 # 抓取所有 enabled 的源并入库
.venv/bin/python -m app.cli fetch --dry-run       # 只打印解析结果，不写 items / fetch_runs
.venv/bin/python -m app.cli fetch --source-id 3   # 只抓某个源（忽略启停，便于单独重试）
.venv/bin/python -m app.cli sources               # 打印源状态表格（启停 / 连败 / 最近成功）
.venv/bin/python -m app.cli pipeline              # 抓取 + 打分一次跑完（手工出报）
.venv/bin/python -m app.cli score                 # 给待打分条目打分（默认上限 200 条）
.venv/bin/python -m app.cli score --limit 3       # 先小批量试一次（换厂商/换模型时用）
.venv/bin/python -m app.cli score --rescore --prompt-version v1  # 删该版本打分并整批重跑
.venv/bin/python -m app.cli purge --before 2026-09-01            # 预览会删多少条（不删任何东西）
.venv/bin/python -m app.cli purge --before 2026-09-01 --yes      # 真删：items 级联删 item_scores
```

铜币画像相关：

```bash
.venv/bin/python -m app.cli coins-parse-orders        # 解析 data/orders.xlsx → 画像（调 LLM，一次性）
.venv/bin/python -m app.cli coins-rebuild-profile     # 只用已入库订单重算画像标签（不调 LLM）
.venv/bin/python -m app.cli coins-tags                # 打印画像标签表格
.venv/bin/python -m app.cli coins-pipeline --limit 20 # 采集 → 匹配 → 定价 一整轮
```

`purge` 删的是 `--before` 那天 `00:00`（业务时区）之前**抓到的**条目（`fetched_at`），
`item_scores` 随外键级联删除，`sources` / `fetch_runs` 保留；不带 `--yes` 时只打印将删除的条数。
全量保留是默认策略，不做自动清理。

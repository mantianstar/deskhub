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

## 启动

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
```

（`uvicorn` 只装在 `.venv` 里，没 `source .venv/bin/activate` 就直接写全路径，否则会 `command not found`。）

开发期建议加 `--reload`（改 `.py` / 模板后自动重载，省掉手工重启；M6 接上调度器后要去掉，重载会打断正在跑的抓取）：

```bash
.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8765
```

想让它不占着终端（关掉终端也继续跑）：

```bash
nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 > data/logs/uvicorn.out 2>&1 &
```

启动时依次执行：加载日志 → 校验配置 → 初始化数据库 → 同步源清单。
配置校验失败会直接退出并打印原因。

打开 <http://127.0.0.1:8765/> 就是当日日报：`digest.limit`（默认 8）条，按 score 降序，
未打分的排在末尾；点标题经 `/go/{id}` 跳原文并记下 `clicked_at`。
当日没有抓到的条目时，页面会回退到最近 `digest.lookback_hours`（默认 48）小时并标注「非今日数据」。

历史搜索在 <http://127.0.0.1:8765/search>（导航栏入口见 tasks.md M5-5）：关键词搜标题 + 摘要，
支持时间范围与入口筛选、分页；筛选条件都在 URL 上，可以直接收藏或分享。
**时间范围按「发布时间」筛**（与卡片上显示的时间同一个口径，plan §12 第 13 条）；
feed 和 URL 都拿不到发布时间的条目，设了时间范围会搜不到，不设时间范围照常能搜到
（美团源原本就是这种，已按 URL 路径里的日期反解，见 plan §12 第 14 条）。

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
也**不会自动抓取**（启动补拉按 plan §11.1 归 M6-1，现在要手工跑 `cli fetch` / `cli pipeline`）。

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
- M2（LLM 打分）：待打分条目（近 7 天、未打分）逐条调用 LLM，产出 `score` + 一句可反驳的 `reason`；单条失败重试 1 次，仍失败则留在池中下轮重试，不写库。当前 112 条真实条目已全部打分。
- M3（日报首页）：`fetch → score` 由 `app/pipeline.py` 编排（`cli pipeline` 手工跑）；首页按 `score` 降序展示当日精选，点击经 `/go/{id}` 记录 `clicked_at`（保留首次）。
- M4（历史搜索）：`/search` 支持关键词（标题 + 摘要 LIKE）+ 时间范围 + 入口筛选 + 分页，结果复用日报卡片；筛选条件都在 URL 上，可分享、可回退。
- 源管理、两个入口视图等外壳能力按 tasks.md 的里程碑顺序推进，尚未实现。

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
```

命令清单随里程碑补齐（`purge` 见 tasks.md M6）。

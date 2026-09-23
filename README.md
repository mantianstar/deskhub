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
uvicorn app.main:app --host 127.0.0.1 --port 8765
```

启动时依次执行：加载日志 → 校验配置 → 初始化数据库 → 同步源清单。
配置校验失败会直接退出并打印原因。

打开 <http://127.0.0.1:8765/> 就是当日日报：`digest.limit`（默认 8）条，按 score 降序，
未打分的排在末尾；点标题经 `/go/{id}` 跳原文并记下 `clicked_at`。
当日没有抓到的条目时，页面会回退到最近 `digest.lookback_hours`（默认 48）小时并标注「非今日数据」。

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
- 搜索、源管理等能力按 tasks.md 的里程碑顺序推进，尚未实现。

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

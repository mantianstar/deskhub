# 运行控制与功能开关 — tasks

> 模块任务与验证证据。目标见 [spec/core.md §9](../spec/core.md)，实现方案见 [plan/core.md §19](../plan/core.md)。
> 通用执行规则（颗粒度 / 提交约定 / 全局禁止项）见 [tasks.md §0](../tasks.md) 与 [RULES.md](../RULES.md)。

## 背景（2026-10-15）

用户提出「每次重启后做的工作梳理下，现在需要经常重启，避免不必要的花费」。梳理结论：

- 重启会自动跑 `pipeline.run`（抓取 + **打分**）与 `coins_pipeline.run`（采集 + 本地匹配 + 查价）。
- 其中**只有打分花 LLM token**；而且打分只取「`item_scores` 里没有记录」的条目（近 7 天），**已打分的不会重打**，
  换模型 / 换 prompt 也不会因重启重跑（那要显式 `score --rescore`）。
- 但每次重启仍会真去抓一遍全部源、把新条目的 token 烧一遍、铜币再采一轮并查价。用户要求：**重启不执行**，
  改成「要跑就手动跑或定时跑，且能按功能关掉」。

## 里程碑（M17）

| # | 块 | 读（必读） | 产出文件 | 依赖 | 验证 |
|---|---|---|---|---|---|
| [x] M17-A | 数据层 + 开关定义：`feature_switches` 表 + `schema_version` 5→6 + repository 读写 + `app/switches.py` | plan §19.2/§19.3、spec §9、`app/models.py`、`app/db.py`、`app/repository.py` | `app/models.py`、`app/db.py`、`app/repository.py`、`app/switches.py` | M16 | 旧库（schema 5）自动抬到 6 并建表；默认全开；toggle 落库后重读仍保留 |
| [x] M17-B | 执行入口 + 启动链路：`app/tasks.py`（四功能 + 手动触发）；删掉 `startup_fetch` / `startup_coins`；`daily_*` 按开关执行 | plan §11.1/§11.2/§19.4、`app/scheduler.py`、`app/main.py` | `app/tasks.py`、`app/scheduler.py`、`app/main.py` | M17-A | 启动日志无「启动补拉」；两个 daily job 仍注册；关掉的开关那一轮跳过 |
| [x] M17-C | 页面：`/control`（开关 + 运行状态 + 最近结果 + 立即运行）+ 导航入口 | plan §19.5、`app/templates/sources.html`（样式参照） | `app/routers/control.py`、`app/templates/control.html`、`app/templates/base.html` | M17-B | 页面列出四个功能；启停 302；未知功能 404；「立即运行」忽略开关、后台跑完结果回显 |
| [x] M17-D | 测试 + 文档回写 | plan/core.md §19.6、spec/core.md §9 / spec/coins.md §14.8、tasks.md | `tests/test_control.py`、`tests/test_scheduler.py`、`tests/conftest.py`、spec.md、plan.md、tasks.md、README.md | M17-C | `pytest` 267 passed；spec↔plan↔tasks↔README 无跑偏 |
| [x] M17-E | 「匹配」进入运行控制（仅手动）：开关定义 + `run_coins_match`（画像优先，无画像回退标签匹配）+ 页面「仅手动」 | plan §19.2/§19.4/§19.5、spec §9 | `app/switches.py`、`app/tasks.py`、`app/templates/control.html`、`tests/test_control.py` | M17-D | 页面列出六个功能；匹配行无启停按钮、可立即运行；`pytest tests -q` 全绿 |

## 验证证据

- 测试：`.venv/bin/python -m pytest tests -q` → `267 passed`。
- 端到端（**临时库 + 8766 端口**，未碰真实库与正在跑的服务）：
  - `GET /healthz` → `200`
  - 启动日志无「启动补拉」，有「调度器已启动：daily_pipeline + daily_coins 每天 08:00（Asia/Shanghai），是否执行以 /control 的开关为准」
  - `GET /control` → 四个功能（知识聚合·抓取 / 打分、铜币拍卖·采集 / 查价）全部渲染
  - `POST /control/knowledge_score/toggle` → `302`，`feature_switches` 落库 `[('knowledge_score', 0)]`
  - `POST /control/nope/toggle`、`POST /control/nope/run` → `404`
- 「立即运行」**没做真机实测**：它会真的抓取 / 打分 / 采集，属于真实网络副作用；按 plan §13.1 只在单测里用桩验证
  （桩函数 + 轮询 `tasks.last_state`）。

## 实测记录（2026-10-15）

- 旧库升级：`data/deskhub.db` 为 schema 5、代码要求 6，属「纯新增表」的向前升级，启动时自动抬版本号、不动数据。
- 为什么开关**默认全开**：用户要的是「重启不执行」，不是「默认关掉定时任务」；07:00 的自动出报保持原行为，想停就在页面上关。
- 页面提示了「关掉打分 → 待打分池堆积 → 重开后一次性补打」这笔集中开销，避免误关后又惊讶 token 变多。

## 实际读取文件清单（RULES §一）

- 必读：`README.md`、`RULES.md`、`tasks.md`、`app/main.py`、`app/scheduler.py`、`app/pipeline.py`、`app/scorer.py`、
  `config/config.yaml`、`app/db.py`、`app/models.py`、`app/routers/sources.py`、`app/templates/sources.html`、
  `app/templates/base.html`、`tests/conftest.py`、`tests/test_scheduler.py`、`tests/test_sources.py`
- 预期波及：`app/repository.py`、`app/coins/pipeline.py`、`app/routers/coins.py`、`spec.md`、`plan.md`、`tasks/coins.md`
- 真探索：无

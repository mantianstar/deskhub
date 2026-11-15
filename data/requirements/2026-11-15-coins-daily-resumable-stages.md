# 需求卡：铜币每日任务分段容错 + 断点续跑 + 评级独立触发

- 类型：L ｜ 日期：2026-11-15 ｜ 状态：待确认（3 项）
- 需求原句：07:00 `daily_coins` 四段串行共用一个 try，任一段失败/取消即停链；改为四段各自独立、单独捕获取消、run_id 落库续跑，并把好品评级拆成 /control 可手动触发的独立动作。
- 只读依据：`app/scheduler.py:57-99`、`app/coins/pipeline.py:105-206`、`app/coins/best.py:89-131`、`app/tasks.py:65-163`、`app/switches.py:42-76`、`app/routers/control.py:30-68`、`app/templates/control.html:37-61`、`app/models.py:7,275-293`、`app/db.py:20-36,62-70`、`app/main.py:55-66`。

## 1. 验收句

1. 令采集段抛 `Exception`（网关 502 替身）→ 日志出现「采集 失败」一条；回采、热度回填、查价三段各被调用 1 次且各自记 ok/failed 状态行。
2. 运行中触发取消（模拟重启）→ 捕获 `asyncio.CancelledError` 后该段记 `cancelled`（含段名），取消继续向上传播（调用方能收到 `CancelledError`），后续段不跑。
3. 重启后 lifespan 启动 → 若库中当日 run 存在 `pending/cancelled/failed` 段，则只为这些段续跑一次；已 `ok` 的段不再调用（以替身调用计数断言）。
4. 同日第二轮触发 `daily_coins` → 已完成段全部跳过，不产生重复采集请求；次日 07:00 生成新 `run_id`，不续前一日。
5. `/control` 出现「评级」行（标记「仅手动」，无启用/停用按钮）→ 点「立即运行」后后台执行，页面显示「完成：匹配 X 条，评级 Y 条」；运行中连点第二次仍 302 且不叠加任务；失败显示「失败：<原因>」。

## 2. 待确认

1. **续跑窗口与触发点**
   - A（推荐）：`lifespan` 启动时查「当日 run 未完成且已过 07:00」→ 后台续跑；跨日未完成一律标 `abandoned`，不续。
   - B：只靠 APScheduler `misfire_grace_time=3600` 兜底，超过 1 小时不补（现状就近）。
   - C：不自动补，仅在 `/control` 显示「未完成」由人点「立即运行」。
2. **run_id 粒度 / 历史记录**
   - A（推荐）：每次 07:00 job 一个新 `run` + 四行 `stage`（collect/finalize/scan/price），`/control` 只显示最近一次，不做历史页。
   - B：每段一条独立 run（无父子关系，续跑需按日期拼）。
   - C：同 A 且 `/control` 增「近 7 天运行记录」列表（需额外模板与查询）。
3. **评级动作的并发互斥**
   - A（推荐）：复用 `tasks._running` 的按 key 互斥（`coins_grade` 单跑，可与采集并行）。
   - B：与采集段互斥（采集在跑时评级排队/拒绝），避免同时写打分表。
   - C：不互斥（风险：两次评级同时消耗每日 50 条配额）。

## 3. 改动链（按依赖顺序）

| # | 文件 | 函数 / 模板 / DDL | 需补测试 |
|---|---|---|---|
| 1 | `app/models.py` | `SCHEMA_VERSION`（:7）→ `"39"`；DDL 在 `coin_fetch_runs`（:275）后新增 `coin_daily_runs(id, run_date, started_at, finished_at, status)` 与 `coin_daily_run_stages(run_id, stage, status, started_at, finished_at, message, PRIMARY KEY(run_id, stage))` | `tests/test_repository.py` 建表断言（待确认是否已有同类断言） |
| 2 | `app/db.py` | `FORWARD_ADDITIVE_UPGRADES`（:23-36）加 `("38","39")`；纯新增表，按注释（:20-22）无需 `v33.py` | — |
| 3 | `app/repository.py` | 新增 `start_coin_daily_run` / `finish_coin_daily_run_stage` / `list_coin_daily_run_stages` / `latest_incomplete_coin_daily_run` / `abandon_coin_daily_run`（唯一 SQL 入口，PROJECT_MAP:57） | `tests/test_repository.py` 新增 2 条 |
| 4 | `app/coins/pipeline.py` | `run`（:105-260）把 :200-206 的好品块改为受参数控制（如 `grade_best: bool = True`）；新增 `grade_only=True` 分支或独立函数 `run_grade_only`，内部 `match_today_lots` + `grade_candidates` | `tests/test_coins_best.py:222` 同口径；`tests/test_coins_fetch.py` 加「grade_only 不采集」 |
| 5 | `app/switches.py` | `FEATURES`（:42-76）加 `COINS_GRADE = "coins_grade"`，`scheduled=False`（照 `COINS_MATCH` :68-74 写法） | `tests/test_control.py:47,95` |
| 6 | `app/tasks.py` | 新增 `run_coins_grade` 并登记进 `RUNNERS`（:103-111）；`trigger/_run`（:139-163）不改 | `tests/test_control.py:117` 补 grade 用例 |
| 7 | `app/scheduler.py` | `daily_coins`（:57-99）拆成 `_STAGES` 列表 + `_run_stage`：每段独立 `try / except asyncio.CancelledError（记 cancelled 后 raise）/ except Exception`；进入 job 时建/取 run_id；新增 `resume_incomplete` 供 lifespan 调用 | `tests/test_scheduler.py` 新增 3 条（:179 现有用例保留） |
| 8 | `app/main.py` | `lifespan`（:55-66）在 `scheduler.start` 后按待确认 1 的规则 `create_task` 续跑（`shutdown(wait=False)` 不变） | `tests/test_scheduler.py` 续跑用例 |
| 9 | `app/routers/control.py` | 不改：`control_page`（:30-47）经 `switches.snapshot()` 自动带出新行；`run_feature`（:59-68）通用 | `tests/test_control.py:87` 断言含「评级」 |
| 10 | `app/templates/control.html` | 不改：`scheduled=False` 已走「仅手动」分支（:38）、「立即运行」表单通用（:58-60） | `tests/test_coins_pages.py`（如需页面断言，待确认归属） |

## 4. 取舍

1. **断点状态放哪**：新建 `coin_daily_runs + coin_daily_run_stages`（推荐，语义为「一天四段的进度」）；复用 `coin_fetch_runs`（:275-286，一条=一次请求，无段顺序与续跑语义，需塞 kind 前缀）；复用 `feature_switches`（只有布尔值，存不下状态）。推荐新建：状态机与请求日志分离，`/control` 查询也简单。
2. **续跑触发**：`lifespan` 读库续跑（推荐，进程重启后 APScheduler 不会重放已错过的 job，`misfire_grace_time` 只覆盖 07:00±1h 且仅在本进程存活时有效，见 `:36,114`）；纯调度器兜底会漏掉「07:01 开跑、09:39 重启」这类场景。
3. **评级独立后的数据依赖**：`grade_candidates`（`best.py:109-131`）只读 `list_pending_grade_lots`，待评级行由 `match_today_lots`（:89-106）写入 → 独立动作必须两步都跑（匹配 + 评级），否则当天采集未跑时点「评级」只会返回 0；不引入「等采集完成再评级」的跨段依赖。

## 5. 明确不做

- 不改 `daily_pipeline`（`app/scheduler.py:39-54`）的语义与异常策略。
- 不改既有测试断言（`tests/test_scheduler.py:179`「daily_coins_swallows_errors」按四段独立后语义仍成立才保留，否则单独提出，不静默改写）。
- 不改 `/control` 页面文案、表格结构与 CSS；不加运行历史前端页面（待确认 2-C 除外）。
- 不改 `feature_switches` 默认值、不改 `DEFAULT_DISABLED`（`app/switches.py:81`）。
- 不动采集限速、网关重试、`MISFIRE_GRACE_SECONDS`；不做异步并行跑四段（仍串行）。
- 不做无关重命名 / 重构（含 `CoinsRunResult`、`RunState` 字段）。

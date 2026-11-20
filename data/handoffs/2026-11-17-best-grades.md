status: done
# 交接：好品推荐改为 CR/AU/MS 三级

## 已完成

- 已出 S 类任务卡（类型 / 需求原句 / 改动链 / 读取清单 / 上限 10 轮）。
- 已定位入口：`app/coins/best.py`（`match_today_lots` L89、`grade_candidates` L109 两阶段、`load_best_lots` 页面取数、常量 `BEST_GRADE_THRESHOLD=40` L27 / `BEST_DAILY_GRADE_LIMIT=20` L28）。
- 已确认 `coin_lot_grades` 写入集中在 `app/repository.py`（`insert_pending_coin_lot_grades` 等），SQL 只允许写在该文件。
- 已确认好品推荐链路相关模块：`app/coin_grade/prompt.py`、`app/coin_grade/mimo_client.py`、`app/coin_grade/parse.py`、`app/coins/recommend.py`、`app/models.py`（`CoinBestLot`）。

## 未完成

- 未读 `grade_candidates` 完整实现（L109 起，只读到 L120），不知道第二阶段评级落库字段与状态流转细节。
- 未确认 `coin_lot_grades` 表结构（是否有 `model_raw`、`net_grade`、`graded_at`、状态字段及取值）。
- 未定位好品推荐页面模板与路由上下文（预计 `app/routers/coins.py` + `app/templates/coins/*.html`，需按符号 `load_best_lots` 找调用点）。
- 三项需求均未改代码：① 第一阶段结果写 `model_raw`、从第 4 步「**CR/AU/MS 辅助清单结果**：偏向」后取等级写 `net_grade`、更新 `graded_at` 与状态；② 删除第二阶段评级；③ 页面口径改为等级（AU/MS）。
- 未跑测试。

## 下一步

1. 读 `app/coins/best.py` L109 之后的 `grade_candidates` 全文（分段 ≤120 行），记录两阶段各自落库字段与状态。
2. 读 `app/models.py` 中 `coin_lot_grades` 建表语句 + `CoinBestLot`，确认 `model_raw` / `net_grade` / `graded_at` / 状态列是否存在，缺列则加迁移（`app/migrations/v*.py` + `app/db.py`）。
3. 读 `app/repository.py` 中相关更新函数（`Grep -n "coin_lot_grades"` 限定该文件），新增/复用「写 model_raw + net_grade + graded_at + 状态」的更新函数。
4. 改 `grade_candidates`：保留第一阶段，落库后解析第 4 步文本取 `偏向` 后等级（CR/AU/MS）写 `net_grade`，删掉第二阶段推理评级调用与 `BEST_GRADE_THRESHOLD` 相关判定。
5. 改 `load_best_lots` 的筛选口径为 `net_grade in ('AU','MS')`（具体以表字段为准），同步页面模板展示口径。
6. 定向跑 `tests/` 中好品推荐相关用例，最后 `pytest -q` 一次。

## 约束

- 只做需求点名的三件事：不改既有文案 / 测试断言，不做无关重命名与重构。
- SQL 只写 `app/repository.py`；建表在 `app/models.py`，迁移走 `app/migrations/`。
- 读取：`Read` 带 `offset/limit`，单段 ≤120 行；文件 >150 行先 `Grep '^(class|def) ' -n` 出符号表。
- 页面改动只改口径（等级 AU/MS），不动布局样式。

## 读取起点

- `app/coins/best.py`（L109 起）
- `app/models.py`（`coin_lot_grades` / `CoinBestLot`）
- `app/repository.py`（`coin_lot_grades` 相关函数）
- `app/coin_grade/parse.py`、`app/coin_grade/prompt.py`（第 4 步文案）
- `app/routers/coins.py` + 好品推荐模板（按 `load_best_lots` 调用点定位）

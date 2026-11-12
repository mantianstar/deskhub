status: done

# 好品推荐三步拆成 CLI 子命令

## 已完成
- 定位 CLI 入口：`app/cli.py`，`_build_parser()` 在 655 行、`main()` 在 815 行、`if __name__` 在 832 行。
- 找到可照抄的样板命令：`cmd_coins_grade`（398 行）、`_bootstrap`（82 行）、`_coins_config`（263 行）。
- 确认 `app/coins/best.py` 三个待拆函数的签名：
  - `match_today_lots(config) -> int`（88 行）
  - `grade_candidates(config, *, client=None) -> int`（103 行）
  - `load_best_lots(config, *, series="") -> list[CoinBestLot]`（166 行）
- 确认现有调用点（改动不得破坏）：`app/coins/pipeline.py:202-203`、`app/routers/coins.py:2106`、`tests/test_coins_best.py:186-218`。

## 未完成
- 未修改任何代码，CLI 子命令尚未新增。

## 下一步
1. 读 `app/cli.py` 655-815（parser 注册区）与 398-435（`cmd_coins_grade` 样板），确认子命令注册写法与 `set_defaults(func=...)` 模式。
2. 新增三个子命令，分别只做一件事：
   - `coins-best-match`：跑 `best.match_today_lots`，打印新入队条数。
   - `coins-best-grade`：跑 `best.grade_candidates`，打印本轮成功评级条数。
   - `coins-best-list`（筛选）：跑 `best.load_best_lots`，用既有 `_print_table` 输出 lot_id / 系列 / 年份 / 分数，支持 `--series`。
3. 保留 `pipeline.run` 里原有的自动串联，不改其行为。
4. 跑 `pytest tests/test_coins_best.py -q` 与 `python -m app.cli --help` 自检。

## 约束
- 只做被要求的：不改既有文案、不改测试断言、不做无关重构。
- 三个子命令各自独立可手工调用，互不隐式串跑。
- 读取遵循 ≤120 行分段；`Grep` 必须带符号名。

## 读取起点
- `app/cli.py:655-815`（`_build_parser`）
- `app/cli.py:398-435`（`cmd_coins_grade`）
- `app/coins/best.py:88-172`

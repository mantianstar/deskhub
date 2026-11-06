status: done

# 2026-11-06 币种映射重跑（会话交接）

- 已完成：讲清拍品 → Numista 币种映射逻辑与落库字段（`coin_lots.series_numista_id / issue_numista_id / match_method / confidence / match_candidates`，迁移 v16）；确认命令行入口为 `coins-map`，`app/cli.py` 有 `__main__` 入口。
- 未完成：尚未实际执行重跑映射；用户只问了做法，未要求跑。
- 下一步：用户确认后执行 `.venv/bin/python -m app.cli coins-map`，核对输出的 `match_method` 分布与「已映射系列/单版」覆盖率。
- 约束：映射规则改动只影响 `app/coins/coinmap.py`；`remap_all()` 只更新有变化的行，可重复执行；不改 `match_score`。
- 读取起点：`app/coins/coinmap.py#L284`（`remap_all`）、`app/cli.py#L344`（`cmd_coins_map`）、`app/cli.py#L661`（子命令注册）。

附：`data/handoffs/2026-11-02-homefilter-sort-probe.md` 仍为 pending，与本任务无关。

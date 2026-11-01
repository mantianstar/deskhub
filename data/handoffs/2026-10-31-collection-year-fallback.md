status: done

# 交接：collection 画像 issue.year 为空时用 min_year/max_year 兜底

## 已完成
- 排查确认 58491/58492/58493 是 CoinSeries.numista_id（1984 建国 35 周年 1 元），其 6 条 issue 的 year / gregorian_year 全为 NULL；根因是 app/coins/collection.py build_year_specs 第 153 行对 year=None 直接 continue，导致画像 years 缺 1984。
- 已出任务卡（S 类，轮数上限 10），用户确认按「min_year == max_year 时填入 year」实现。

## 未完成
- app/coins/collection.py 代码改动。
- tests/test_coins_collection.py 新增用例。

## 下一步
1. 改 app/coins/collection.py build_year_specs（143–178）：循环开头取 `year = issue.year`；若 `year is None and series is not None and series.min_year is not None and series.min_year == series.max_year`，则 `year = series.min_year`；随后仍是 `year is None` 或 `is_proof_issue(...)` 则 continue。CoinYearSpec.year 用兜底年份，gregorian_year 保持 issue.gregorian_year（不做推断）。
2. 在 tests/test_coins_collection.py 的 test_year_specs_pick_max_mintage_and_exclude_proof（244 行）后加一条用例：series 的 min_year=max_year=1984、issue.year=None，断言 specs 含 1984；再补一条 min_year != max_year 时不兜底。
3. 跑 `.venv/bin/python -m pytest tests/test_coins_collection.py -q`。

## 约束
- 只改 build_year_specs 这一处，不动 explore.py / recommend.py 的同类口径。
- 不改既有测试断言；不新增文档。
- 任务收口出四行回执。

## 读取起点
- app/coins/collection.py:143-178（build_year_specs）
- app/models.py:787-817（CoinSeries.min_year / max_year）
- tests/test_coins_collection.py:244-263（既有 build_year_specs 用例与 _series/_issue 用法）

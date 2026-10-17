"""v7 → v8：`coin_issues` / `coin_issues_cn` 加 `gregorian_year`（公历年份）。

单版接口同时给 `year`（原值，如明治 6 年 = 6）与 `gregorian_year`（公历 1873），
老库只存了原值；补列后重新采集即可并显。
"""

from __future__ import annotations

COLUMNS: dict[str, dict[str, str]] = {
    "coin_issues": {"gregorian_year": "INTEGER"},
    "coin_issues_cn": {"gregorian_year": "INTEGER"},
}

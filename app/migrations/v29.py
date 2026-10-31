"""v29：给币库系列表加「发行系列 / 发行主题」两列。

两列由系列详情页人工维护，采集（Numista upsert）不写这两列，重采不会覆盖人工值。
"""

from __future__ import annotations

COLUMNS = {
    "coin_series": {
        "issue_series": "TEXT NOT NULL DEFAULT ''",
        "issue_theme": "TEXT NOT NULL DEFAULT ''",
    },
    "coin_series_cn": {
        "issue_series": "TEXT NOT NULL DEFAULT ''",
        "issue_theme": "TEXT NOT NULL DEFAULT ''",
    },
}

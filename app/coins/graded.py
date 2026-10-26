"""评级盒判定（plan/coins.md §18.19.6）。

站点没有「是否评级」字段，只能按标题判定：命中评级机构或「评级 / 盒」即视为评级盒，
其余为裸币。只做布尔，不解析品相与评级分数（P1-5 缩窄版）。
"""

from __future__ import annotations

import re

# PCGS / NGC / 公博 / 华夏 / 保粹 / 爱藏 / 闻德 / 众诚 / 评级 / 盒
_GRADED_RE = re.compile(r"PCGS|NGC|公博|华夏|保粹|爱藏|闻德|众诚|评级|盒", re.IGNORECASE)


def is_graded_title(title: str | None) -> int:
    """标题 → 1（评级盒）/ 0（裸币）。"""
    return 1 if title and _GRADED_RE.search(title) else 0

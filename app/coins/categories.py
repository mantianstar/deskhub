"""站点拍品分类的**种子字典**（plan/coins.md §18.19.2 / §18.19.3）。

站点没有分类列表接口（相关 action 全部 403，2026-10-26 实测），名称只能人工维护在这里；
采集与回填把字典 upsert 进 `coin_categories`，字典**外**的码以 `name = code` 兜底并记日志，
这样站点新增分类时不用改代码也能先落库。

口径：`coin_lots` 只存**最细一级** —— `cate_id_2` 有值取它，否则回落 `cate_id_1`，
0 / 缺失记 NULL；一级码由前缀推导（`code[:3] + "00"`），不单独存两列。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from app import repository

logger = logging.getLogger(__name__)

# 一级码 `cate_id_1`（扫描 10000–24000，步长 1000；plan §18.19.2）
LEVEL1: dict[int, str] = {
    10000: "钞票",
    11000: "硬币",
    12000: "邮票",
    13000: "章牌",
    14000: "书籍",
    15000: "空",
    16000: "珠宝",
    17000: "陶瓷",
    18000: "文玩",
    19000: "书画",
    20000: "茶酒",
    21000: "潮玩",
    22000: "工艺",
    23000: "卡片",
    24000: "杂项 / 其他",
}

# 硬币二级码 `cate_id_2`（父 11000）：本轮只需硬币这一支（plan §18.19.2）
LEVEL2: dict[int, str] = {
    11010: "现代币",
    11020: "纪念币",
    11030: "古钱币",
    11040: "铜元",
    11050: "银元",
    11060: "金币",
    11070: "外币",
    11080: "金银锭",
    11090: "打制币",
}

# 字典外的码排在最后，方便在库里一眼看出是哪几个
UNKNOWN_SORT_BASE = 900_000

_SEED_CODES = {str(code) for code in (*LEVEL1, *LEVEL2)}


def _sort_for(code: str) -> int:
    return int(code) if code.isdigit() else UNKNOWN_SORT_BASE


def seed_rows() -> list[tuple[str, str, int]]:
    """种子字典 → `(code, name, sort)` 行（一级在前、二级在后，sort 取码值）。"""
    return [
        (str(code), name, _sort_for(str(code)))
        for code, name in (*LEVEL1.items(), *LEVEL2.items())
    ]


def name_for(code: str | None) -> str | None:
    """分类码 → 名称；字典外返回 None（落库时由 `ensure_codes` 兜底成码本身）。"""
    if not code or not code.isdigit():
        return None
    number = int(code)
    return LEVEL2.get(number) or LEVEL1.get(number)


def parent_code(code: str | None) -> str | None:
    """二级码 → 一级码（`code[:3] + "00"`）；一级码原样返回。"""
    if not code or not code.isdigit() or len(code) < 3:
        return None
    parent = code[:3] + "00"
    return parent if parent != code else code


def sync_seed() -> None:
    """把种子字典写进 `coin_categories`（按 `code` upsert name / sort）。"""
    repository.upsert_coin_categories(seed_rows())


def ensure_codes(codes: Iterable[str | None]) -> None:
    """确保这些分类码在 `coin_categories` 里有行（外键要求），字典外的码兜底成码本身。"""
    missing = sorted({code for code in codes if code and code not in _SEED_CODES})
    if not missing:
        return
    logger.warning("分类字典外的码：%s（以 name=code 兜底）", "、".join(missing))
    repository.upsert_coin_categories([(code, code, _sort_for(code)) for code in missing])

"""站点拍品分类：`miniprogram.cate.list` 同步 + 种子兜底（plan/coins.md §18.19.2 / §18.19.3）。

接口返回一级 `cates[]` 与二级 `sub[]`，每日采集前由 `sync()` 同步一次落进
`coin_categories`；接口失败 / 返回空时回落到下面的种子字典，字典**外**的码仍以
`name = code` 兜底并记日志，这样站点增删分类不用改代码也能先落库。

口径：`coin_lots` 只存**最细一级** —— `cate_id_2` 有值取它，否则回落 `cate_id_1`，
0 / 缺失记 NULL；一级码由前缀推导（`code[:3] + "00"`），不单独存两列。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from app import repository
from app.coins.gapi import GapiError

if TYPE_CHECKING:
    from app.coins.gapi import MxiqiGapi

logger = logging.getLogger(__name__)

CategoryRow = tuple[
    str, str, int, str | None, int, str | None, int | None, int | None
]

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


def _sort_for(code: str) -> int:
    return int(code) if code.isdigit() else UNKNOWN_SORT_BASE


def seed_rows() -> list[CategoryRow]:
    """种子字典 → 完整分类行（一级在前、二级在后；`sort` / `order_no` 取码值）。"""
    rows: list[CategoryRow] = [
        (str(code), name, _sort_for(str(code)), None, 1, None, None, code)
        for code, name in LEVEL1.items()
    ]
    rows += [
        (
            str(code),
            name,
            _sort_for(str(code)),
            parent_code(str(code)),
            2,
            None,
            None,
            code,
        )
        for code, name in LEVEL2.items()
    ]
    return rows


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
    """种子字典写进 `coin_categories`（接口失败时的兜底；按 `code` upsert）。"""
    repository.upsert_coin_categories(seed_rows())


async def sync(api: "MxiqiGapi") -> None:
    """每日采集前同步站点分类字典；接口失败 / 空结果时回落种子字典。"""
    try:
        items = await api.list_categories()
    except GapiError as exc:
        logger.warning("分类字典接口失败，回落种子字典：%s", exc)
        sync_seed()
        return
    if not items:
        logger.warning("分类字典接口返回空，回落种子字典")
        sync_seed()
        return
    repository.upsert_coin_categories(
        [
            (
                item.code,
                item.name,
                item.order_no if item.order_no is not None else _sort_for(item.code),
                item.parent_code,
                item.level,
                item.thumb,
                item.item_count,
                item.order_no,
            )
            for item in items
        ],
        prune=True,
    )


def ensure_codes(codes: Iterable[str | None]) -> None:
    """确保这些分类码在 `coin_categories` 里有行（外键要求），只补缺行。

    「缺失」以库里的为准：接口同步进来的名称一律不动，只有库里没有的码才以
    `name = code` 兜底。
    """
    candidates = sorted({code for code in codes if code})
    existing = repository.existing_coin_category_codes(candidates)
    missing = [code for code in candidates if code not in existing]
    if not missing:
        return
    logger.warning("分类字典里没有的码：%s（以 name=code 兜底）", "、".join(missing))
    rows: list[CategoryRow] = []
    for code in missing:
        parent = parent_code(code)
        rows.append(
            (
                code,
                code,
                _sort_for(code),
                None if parent == code else parent,
                1 if parent == code else 2,
                None,
                None,
                None,
            )
        )
    repository.upsert_coin_categories(rows)

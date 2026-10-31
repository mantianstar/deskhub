"""按版本登记的迁移（plan §6.2）。

`db.init_db` 在建表后调用这里：`CREATE TABLE IF NOT EXISTS` 只能**新增表**，
给老表**加列**（SQLite 不支持 ADD COLUMN IF NOT EXISTS）必须走 ALTER —— 就是本包登记的迁移。

登记表按 (from_version, to_version) 索引；应用是**幂等**的（列已存在则跳过），
因此重复执行安全，也不会与新库的 DDL（已含新列）冲突。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from app.migrations import (
    v4,
    v8,
    v11,
    v12,
    v13,
    v14,
    v16,
    v17,
    v18,
    v19,
    v20,
    v21,
    v22,
    v23,
    v24,
    v26,
    v27,
    v28,
    v29,
)

# (from_version, to_version) -> {表名: {列名: 列定义}}
COLUMN_MIGRATIONS: dict[tuple[str, str], dict[str, dict[str, str]]] = {
    ("3", "4"): v4.COLUMNS,
    ("7", "8"): v8.COLUMNS,
    ("10", "11"): v11.COLUMNS,
    ("12", "13"): v13.COLUMNS,
    ("13", "14"): v14.COLUMNS,
    ("15", "16"): v16.COLUMNS,
    ("16", "17"): v17.COLUMNS,
    ("17", "18"): v18.COLUMNS,
    ("18", "19"): {},
    ("19", "20"): {},
    ("20", "21"): v21.COLUMNS,
    # 隔一版的库（v20）直接升到 v22：把两步加列合并（v21 无数据回填）
    ("20", "22"): {
        "coin_lots": {**v21.COLUMNS["coin_lots"], **v22.COLUMNS["coin_lots"]},
    },
    ("21", "22"): v22.COLUMNS,
    # 越过 v22 的库直接升到 v23：低位版本把 v21 / v22 的加列一并带上（v23 只加分类字典列）
    ("20", "23"): {
        "coin_lots": {**v21.COLUMNS["coin_lots"], **v22.COLUMNS["coin_lots"]},
        "coin_categories": v23.COLUMNS["coin_categories"],
    },
    ("21", "23"): {
        "coin_lots": v22.COLUMNS["coin_lots"],
        "coin_categories": v23.COLUMNS["coin_categories"],
    },
    ("22", "23"): v23.COLUMNS,
    # v24 只给 coin_lots 加热度明细列；跨版本升级把中间版本的列一并带上
    ("20", "24"): {
        "coin_lots": {
            **v21.COLUMNS["coin_lots"],
            **v22.COLUMNS["coin_lots"],
            **v24.COLUMNS["coin_lots"],
        },
        "coin_categories": v23.COLUMNS["coin_categories"],
    },
    ("21", "24"): {
        "coin_lots": {**v22.COLUMNS["coin_lots"], **v24.COLUMNS["coin_lots"]},
        "coin_categories": v23.COLUMNS["coin_categories"],
    },
    ("22", "24"): {
        "coin_lots": v24.COLUMNS["coin_lots"],
        "coin_categories": v23.COLUMNS["coin_categories"],
    },
    ("23", "24"): v24.COLUMNS,
    # v25 只新增断点表（DDL 已建），跨版本升级仍要把中间版本的加列一并带上
    ("20", "25"): {
        "coin_lots": {
            **v21.COLUMNS["coin_lots"],
            **v22.COLUMNS["coin_lots"],
            **v24.COLUMNS["coin_lots"],
        },
        "coin_categories": v23.COLUMNS["coin_categories"],
    },
    ("21", "25"): {
        "coin_lots": {**v22.COLUMNS["coin_lots"], **v24.COLUMNS["coin_lots"]},
        "coin_categories": v23.COLUMNS["coin_categories"],
    },
    ("22", "25"): {
        "coin_lots": v24.COLUMNS["coin_lots"],
        "coin_categories": v23.COLUMNS["coin_categories"],
    },
    ("23", "25"): v24.COLUMNS,
    # v26 给系列加「所属系列 / 纪念主题」两列；跨版本升级把中间版本的加列一并带上
    ("20", "26"): {
        "coin_lots": {
            **v21.COLUMNS["coin_lots"],
            **v22.COLUMNS["coin_lots"],
            **v24.COLUMNS["coin_lots"],
        },
        "coin_categories": v23.COLUMNS["coin_categories"],
        **v26.COLUMNS,
    },
    ("21", "26"): {
        "coin_lots": {**v22.COLUMNS["coin_lots"], **v24.COLUMNS["coin_lots"]},
        "coin_categories": v23.COLUMNS["coin_categories"],
        **v26.COLUMNS,
    },
    ("22", "26"): {
        "coin_lots": v24.COLUMNS["coin_lots"],
        "coin_categories": v23.COLUMNS["coin_categories"],
        **v26.COLUMNS,
    },
    ("23", "26"): {**v24.COLUMNS, **v26.COLUMNS},
    ("24", "26"): v26.COLUMNS,
    ("25", "26"): v26.COLUMNS,
    # v27 给品相表 / 收藏系列加「主题 / 名称」两列；跨版本升级把中间版本的加列一并带上
    ("20", "27"): {
        "coin_lots": {
            **v21.COLUMNS["coin_lots"],
            **v22.COLUMNS["coin_lots"],
            **v24.COLUMNS["coin_lots"],
        },
        "coin_categories": v23.COLUMNS["coin_categories"],
        **v26.COLUMNS,
        **v27.COLUMNS,
    },
    ("21", "27"): {
        "coin_lots": {**v22.COLUMNS["coin_lots"], **v24.COLUMNS["coin_lots"]},
        "coin_categories": v23.COLUMNS["coin_categories"],
        **v26.COLUMNS,
        **v27.COLUMNS,
    },
    ("22", "27"): {
        "coin_lots": v24.COLUMNS["coin_lots"],
        "coin_categories": v23.COLUMNS["coin_categories"],
        **v26.COLUMNS,
        **v27.COLUMNS,
    },
    ("23", "27"): {**v24.COLUMNS, **v26.COLUMNS, **v27.COLUMNS},
    ("24", "27"): {**v26.COLUMNS, **v27.COLUMNS},
    ("25", "27"): {**v26.COLUMNS, **v27.COLUMNS},
    ("26", "27"): v27.COLUMNS,
    # v28 把主题 / 名称下推到收藏系列成员
    ("26", "28"): {**v27.COLUMNS, **v28.COLUMNS},
    ("27", "28"): v28.COLUMNS,
    ("28", "29"): v29.COLUMNS,
}

# (from_version, to_version) -> 建表之外还要跑一次的数据回填
DATA_MIGRATIONS: dict[tuple[str, str], Callable[[sqlite3.Connection], None]] = {
    ("11", "12"): v12.rebuild_coin_lot_fts,
    ("17", "18"): v18.backfill_coin_lot_images,
    ("18", "19"): v19.normalize_series_labels,
    ("19", "20"): v20.upgrade_japanese_grade_years,
    ("20", "22"): v22.backfill_is_graded,
    ("21", "22"): v22.backfill_is_graded,
    ("20", "23"): v22.backfill_is_graded,
    ("21", "23"): v22.backfill_is_graded,
    ("20", "24"): v22.backfill_is_graded,
    ("21", "24"): v22.backfill_is_graded,
    ("20", "25"): v22.backfill_is_graded,
    ("21", "25"): v22.backfill_is_graded,
    ("20", "26"): v26.upgrade_from_20,
    ("21", "26"): v26.upgrade_from_21,
    ("22", "26"): v26.cleanup_china_and_countries,
    ("23", "26"): v26.cleanup_china_and_countries,
    ("24", "26"): v26.cleanup_china_and_countries,
    ("25", "26"): v26.cleanup_china_and_countries,
    ("20", "27"): v27.upgrade_from_20,
    ("21", "27"): v27.upgrade_from_21,
    ("22", "27"): v27.upgrade_from_22,
    ("23", "27"): v27.upgrade_from_22,
    ("24", "27"): v27.upgrade_from_22,
    ("25", "27"): v27.upgrade_from_22,
    ("26", "27"): v27.upgrade_from_26,
    ("26", "28"): v28.upgrade_from_26,
    ("27", "28"): v28.push_down,
}


def _then_steps(
    first: Callable[[sqlite3.Connection], None],
    second: Callable[[sqlite3.Connection], None],
) -> Callable[[sqlite3.Connection], None]:
    """把两段数据迁移按顺序串起来（跨版本直升级用）。"""

    def run(conn: sqlite3.Connection) -> None:
        first(conn)
        second(conn)

    return run


# 跨版本直升级到 v28：加列沿用 v27 的组合，数据迁移先跑 v27 的回填，再下推主题 / 名称
for _from in ("20", "21", "22", "23", "24", "25"):
    COLUMN_MIGRATIONS[(_from, "28")] = {
        **COLUMN_MIGRATIONS[(_from, "27")],
        **v28.COLUMNS,
    }
    DATA_MIGRATIONS[(_from, "28")] = _then_steps(
        DATA_MIGRATIONS[(_from, "27")], v28.push_down
    )

# 跨版本直升级到 v29：沿用 v28 的加列与回填，再补发行系列 / 发行主题两列
for _from in ("20", "21", "22", "23", "24", "25", "26", "27"):
    COLUMN_MIGRATIONS[(_from, "29")] = {
        **COLUMN_MIGRATIONS[(_from, "28")],
        **v29.COLUMNS,
    }
    DATA_MIGRATIONS[(_from, "29")] = DATA_MIGRATIONS[(_from, "28")]


def apply_columns(conn: sqlite3.Connection, tables: dict[str, dict[str, str]]) -> list[str]:
    """给老表补列（已存在的列跳过）。返回实际新增的 `表.列` 列表。"""
    applied: list[str] = []
    for table, columns in tables.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            applied.append(f"{table}.{name}")
    return applied

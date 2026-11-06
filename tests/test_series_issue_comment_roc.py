"""系列 1587（10 新元）单版备注的民国年号换算。

页面链路：/coins/series/{id} → repository.get_coin_series(cn=True)
→ repository.list_coin_issues(series.id, cn=True) → _row_to_coin_issue
→ translate.fix_reversed_roc_legend(comment) → coins/series.html:169。

库内 coin_issues_cn 中 (year=70, gregorian_year=1981) 的 comment 实存简体
「中华民国十七年」（不是倒序繁体「年十七國民華中」），页面原样显示。
按 gregorian_year=1981 应为「中华民国七十年」。
"""

import sqlite3

from app import repository

_COLUMNS = (
    "id, numista_id, series_id, year, gregorian_year, mint, mint_mark, variety, "
    "comment, mintage, size_mm, weight_g, thickness_mm, edge, is_proof, is_essai, "
    "picture, catalog_no, fetched_at"
)


def _issue_row(**overrides: object) -> sqlite3.Row:
    values = {
        "id": 2189,
        "numista_id": 1587,
        "series_id": 463,
        "year": 70,
        "gregorian_year": 1981,
        "mint": None,
        "mint_mark": None,
        "variety": None,
        "comment": "中华民国十七年",
        "mintage": None,
        "size_mm": None,
        "weight_g": None,
        "thickness_mm": None,
        "edge": None,
        "is_proof": 0,
        "is_essai": 0,
        "picture": None,
        "catalog_no": None,
        "fetched_at": "2026-01-01 00:00:00",
    }
    values.update(overrides)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE row_fixture ({_COLUMNS})")
    conn.execute(
        f"INSERT INTO row_fixture ({_COLUMNS}) VALUES ({', '.join('?' * len(values))})",
        tuple(values.values()),
    )
    return conn.execute("SELECT * FROM row_fixture").fetchone()


def test_series_1587_issue_comment_uses_roc_year_from_gregorian() -> None:
    issue = repository._row_to_coin_issue(_issue_row())
    assert issue.comment == "中华民国七十年"

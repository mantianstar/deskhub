"""拍品 → Numista 币种映射（plan §18.18.5）。纯函数为主，不碰站点。"""

from __future__ import annotations

import pytest

from app import db, repository
from app.coins import coinmap
from app.migrations import v32
from app.models import CoinLot, CoinSession, CoinShop

_FETCHED_AT = "2026-10-22T00:00:00Z"


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


def _catalogue(
    *series: coinmap.SeriesFacts,
    issue_years: dict[int, dict[int, list[int]]] | None = None,
    issuers: tuple[tuple[str, str], ...] = (("英国", "英国"), ("美国", "美国")),
) -> coinmap.Catalogue:
    return coinmap.Catalogue(
        series=series,
        issuers=issuers,
        issue_years=issue_years or {},
    )


def _penny(numista_id: int, min_year: int, max_year: int) -> coinmap.SeriesFacts:
    return coinmap.SeriesFacts(
        numista_id=numista_id,
        country="英国",
        min_year=min_year,
        max_year=max_year,
        denomination="1便士",
    )


def test_attrs_win_and_unique_issue_is_written():
    catalogue = _catalogue(_penny(578, 1911, 1926), issue_years={578: {1919: [9001]}})

    mapping = coinmap.match_lot(
        "英国1919年1便士", {"国家地区": "英国", "发行年份": "1919"}, catalogue
    )

    assert mapping.match_method == "attrs"
    assert mapping.series_numista_id == 578
    assert mapping.issue_numista_id == 9001
    assert mapping.confidence == 0.95


def test_title_path_leaves_issue_empty_when_several_varieties():
    # 同年多个版别：系列可信，单版留空并记候选（宁缺勿错）
    catalogue = _catalogue(_penny(578, 1911, 1926), issue_years={578: {1919: [9001, 9002]}})

    mapping = coinmap.match_lot("英国1919年1便士 好品", {}, catalogue)

    assert mapping.match_method == "title"
    assert mapping.series_numista_id == 578
    assert mapping.issue_numista_id is None
    assert mapping.candidates == (9001, 9002)
    assert mapping.confidence == 0.7


def test_multiple_series_is_not_mapped():
    catalogue = _catalogue(_penny(578, 1911, 1926), _penny(669, 1911, 1926))

    mapping = coinmap.match_lot("英国1919年1便士", {}, catalogue)

    assert mapping.match_method == "none"
    assert mapping.series_numista_id is None
    assert mapping.issue_numista_id is None
    assert mapping.candidates == ()


def test_multi_coin_title_is_not_mapped():
    catalogue = _catalogue(_penny(578, 1911, 1926))

    mapping = coinmap.match_lot("英国1919年1便士2枚", {}, catalogue)

    assert mapping.match_method == "none"
    assert mapping.series_numista_id is None


def test_title_era_year_falls_back_to_gregorian():
    catalogue = _catalogue(
        coinmap.SeriesFacts(
            numista_id=1, country="日本", min_year=1926, max_year=1989, denomination="1钱"
        ),
        issuers=(("日本", "日本"),),
    )

    mapping = coinmap.match_lot("日本昭和十五年1钱", {}, catalogue)

    assert mapping.match_method == "title"
    assert mapping.series_numista_id == 1


def test_title_chinese_numeral_denomination_is_mapped():
    # 标题写「二钱」，币库是「2 钱」：中文数字归一后仍能对上
    catalogue = _catalogue(
        coinmap.SeriesFacts(
            numista_id=2317, country="日本", min_year=1873, max_year=1892, denomination="2钱"
        ),
        issuers=(("日本", "日本"),),
    )

    mapping = coinmap.match_lot("日本明治十五年龙二钱", {}, catalogue)

    assert mapping.match_method == "title"
    assert mapping.series_numista_id == 2317


def test_title_chinese_numeral_one_penny_is_mapped():
    catalogue = _catalogue(
        coinmap.SeriesFacts(
            numista_id=1591, country="新西兰", min_year=1953, max_year=1965, denomination="1便士"
        ),
        issuers=(("新西兰", "新西兰"),),
    )

    mapping = coinmap.match_lot("新西兰1964年一便士铜币", {}, catalogue)

    assert mapping.series_numista_id == 1591


def test_title_half_fraction_denomination_is_mapped():
    # 标题写「1/2便士」，币库归一成「半 便士」：要落到半便士，不能落到 1 便士
    catalogue = _catalogue(
        coinmap.SeriesFacts(
            numista_id=3203, country="南非", min_year=1953, max_year=1960, denomination="1便士"
        ),
        coinmap.SeriesFacts(
            numista_id=9968, country="南非", min_year=1953, max_year=1960, denomination="半便士"
        ),
        issuers=(("南非", "南非"),),
    )

    mapping = coinmap.match_lot("1960英属南非1/2便士铜币 好品", {}, catalogue)

    assert mapping.match_method == "title"
    assert mapping.series_numista_id == 9968


def test_title_japanese_era_infers_country():
    # 标题没写国家、标签也为空：日本年号（明治）推断出日本
    catalogue = _catalogue(
        coinmap.SeriesFacts(
            numista_id=2317, country="日本", min_year=1873, max_year=1892, denomination="2钱"
        )
    )

    mapping = coinmap.match_lot("明治十五年龙二钱", {}, catalogue)

    assert mapping.match_method == "title"
    assert mapping.series_numista_id == 2317


def test_roc_era_does_not_infer_japan():
    # 民国纪年不是日本年号，不能推断成日本
    catalogue = _catalogue(
        coinmap.SeriesFacts(
            numista_id=2317, country="日本", min_year=1970, max_year=1990, denomination="1元"
        )
    )

    mapping = coinmap.match_lot("民国七十年1元", {}, catalogue)

    assert mapping.series_numista_id is None


def test_denomination_conflict_is_not_mapped():
    # 标题写 25 美分，币库只有 1 分：不能记到 1 分系列上
    catalogue = _catalogue(
        coinmap.SeriesFacts(
            numista_id=1, country="美国", min_year=1965, max_year=1998, denomination="1分"
        )
    )

    mapping = coinmap.match_lot("美国1980年25美分", {}, catalogue)

    assert mapping.match_method == "none"
    assert mapping.series_numista_id is None


def test_missing_year_is_not_mapped():
    catalogue = _catalogue(_penny(578, 1911, 1926))

    mapping = coinmap.match_lot("英国1便士铜币", {}, catalogue)

    assert mapping.match_method == "none"


def test_missing_denomination_is_not_mapped():
    # 只有国家 + 年份，标题没写面值：可能把「1 弗洛林」记到便士系列上，宁可不映射
    catalogue = _catalogue(_penny(578, 1911, 1926))

    mapping = coinmap.match_lot("英国1919年银币", {}, catalogue)

    assert mapping.match_method == "none"


def test_remap_all_on_empty_db(temp_db):
    result = coinmap.remap_all()

    assert result.lots_total == 0
    assert result.lots_series_mapped == 0
    assert result.lots_issue_mapped == 0


def test_remap_all_writes_mapping_and_keeps_multi_issue_empty(temp_db):
    shop_id = repository.upsert_coin_shop(
        CoinShop(
            id=None,
            mxiqi_uid="1",
            name="店",
            avatar_url=None,
            badges=[],
            fetched_at=_FETCHED_AT,
        )
    )
    session_id = repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id="s1",
            shop_id=shop_id,
            title="专场",
            status="running",
            auction_at=None,
            auction_at_raw=None,
            deposit=None,
            lot_total=1,
            view_count=0,
            cover_urls=[],
            fetched_at=_FETCHED_AT,
        )
    )
    repository.upsert_coin_lot(
        CoinLot(
            id=None,
            session_id=session_id,
            mxiqi_id="l1",
            lot_no=None,
            title="英国1919年1便士",
            start_price=1.0,
            current_price=0.0,
            image_urls=[],
            tags={},
            match_score=0.0,
            is_recommended=False,
            fetched_at=_FETCHED_AT,
        )
    )

    catalogue = _catalogue(_penny(578, 1911, 1926), issue_years={578: {1919: [9001, 9002]}})
    result = coinmap.remap_all(catalogue)

    assert result.lots_total == 1
    assert result.lots_series_mapped == 1
    assert result.lots_issue_mapped == 0
    assert result.by_method == {"title": 1}


def _insert_grade(country: str, era: str, denomination: str, grade: str) -> None:
    with db.connect() as conn, conn:
        conn.execute(
            "INSERT INTO coin_purchase_grades "
            "(country, era, denomination, grade, updated_at, theme, coin_name) "
            "VALUES (?, ?, ?, ?, ?, '', '')",
            (country, era, denomination, grade, _FETCHED_AT),
        )


def test_v32_normalizes_legacy_grade_keys(temp_db):
    _insert_grade("中国", "1987", "1 圆", "好")

    with db.connect() as conn, conn:
        v32.normalize_grade_keys(conn)

    with db.connect() as conn:
        row = conn.execute("SELECT denomination FROM coin_purchase_grades").fetchone()
    assert row["denomination"] == "1 元"


def test_v32_keeps_conflicting_legacy_row(temp_db):
    _insert_grade("中国", "1987", "1 圆", "差")
    _insert_grade("中国", "1987", "1 元", "好")

    with db.connect() as conn, conn:
        v32.normalize_grade_keys(conn)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT denomination, grade FROM coin_purchase_grades ORDER BY id"
        ).fetchall()
    assert [(row["denomination"], row["grade"]) for row in rows] == [
        ("1 圆", "差"),
        ("1 元", "好"),
    ]

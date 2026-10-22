"""拍品 → Numista 币种映射（plan §18.18.5）。纯函数为主，不碰站点。"""

from __future__ import annotations

import pytest

from app import db, repository
from app.coins import coinmap
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


def test_multiple_series_store_candidates_only():
    catalogue = _catalogue(_penny(578, 1911, 1926), _penny(669, 1911, 1926))

    mapping = coinmap.match_lot("英国1919年1便士", {}, catalogue)

    assert mapping.match_method == "title_multi"
    assert mapping.series_numista_id is None
    assert mapping.issue_numista_id is None
    assert mapping.candidates == (578, 669)
    assert mapping.confidence == 0.5


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

"""收藏画像与推荐优先级测试（spec §14.17）。

运行方式：`.venv/bin/python -m pytest tests/test_coins_collection.py -q`
纯函数 + 临时库，不发网络请求。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import config, db, repository
from app.coins import collection, explore, recommend
from app.coins.numista import SeriesInfo
from app.main import app as fastapi_app
from app.models import (
    CoinCollectionProfile,
    CoinCollectionSeries,
    CoinExploreCandidate,
    CoinIssue,
    CoinIssueValues,
    CoinLot,
    CoinOrder,
    CoinPurchaseGrade,
    CoinRecommendation,
    CoinSeries,
    CoinSession,
    CoinTodayLot,
    CoinYearSpec,
)


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


def _series(
    numista_id: int,
    *,
    issuer: str = "英国",
    ruler: str = "维多利亚",
    denomination: str = "1 便士",
    size_mm: float = 31.0,
    composition: str = "Bronze",
    min_year: int = 1860,
    max_year: int = 1864,
    comments: str | None = None,
) -> CoinSeries:
    return CoinSeries(
        id=None,
        numista_id=numista_id,
        title=f"{issuer} {denomination}",
        url=None,
        object_type="标准流通币",
        issuer=issuer,
        min_year=min_year,
        max_year=max_year,
        ruler=ruler,
        denomination=denomination,
        size_mm=size_mm,
        weight_g=9.4,
        thickness_mm=None,
        shape="圆形",
        composition=composition,
        edge=None,
        obverse_desc=None,
        obverse_lettering=None,
        obverse_picture=None,
        reverse_desc=None,
        reverse_lettering=None,
        reverse_picture=None,
        catalog_no=None,
        comments=comments,
        mints=[],
        fetched_at=repository.utcnow(),
    )


def _issue(
    numista_id: int,
    series_id: int,
    *,
    year: int,
    mintage: int | None = 1000,
    gregorian_year: int | None = None,
    is_proof: bool | None = False,
    comment: str | None = None,
    size_mm: float | None = 31.0,
) -> CoinIssue:
    return CoinIssue(
        id=None,
        numista_id=numista_id,
        series_id=series_id,
        year=year,
        gregorian_year=gregorian_year if gregorian_year is not None else year,
        mint=None,
        mint_mark=None,
        variety=None,
        comment=comment,
        mintage=mintage,
        size_mm=size_mm,
        weight_g=9.4,
        thickness_mm=None,
        edge=None,
        is_proof=is_proof,
        is_essai=None,
        picture=None,
        catalog_no=None,
        fetched_at=repository.utcnow(),
    )


def _values(
    numista_id: int, series_id: int, *, frequency: float | None = None, vf: float | None = None, xf: float | None = None
) -> CoinIssueValues:
    return CoinIssueValues(
        id=None,
        numista_id=numista_id,
        series_numista_id=series_id,
        currency="CNY",
        value_g=None,
        value_vg=None,
        value_f=None,
        value_vf=vf,
        value_xf=xf,
        value_au=None,
        value_unc=None,
        frequency=frequency,
        fetched_at=repository.utcnow(),
    )


def _collection(key: str = "uk-victoria-penny") -> CoinCollectionSeries:
    now = repository.utcnow()
    return CoinCollectionSeries(
        id=1,
        collection_key=key,
        name="英国 · 维多利亚 · 1 便士",
        country="英国",
        ruler="维多利亚",
        denomination="1 便士",
        size_mm=None,
        size_band=None,
        created_at=now,
        updated_at=now,
    )


def _profile(
    *,
    key: str = "uk-victoria-penny",
    country: str = "英国",
    ruler: str = "维多利亚",
    denomination: str = "1 便士",
    size_mm: float = 31.0,
    band: str = "大",
    years: list[int] | None = None,
    owned: list[int] | None = None,
    upgrade: list[int] | None = None,
    specs: dict[int, CoinYearSpec] | None = None,
) -> CoinCollectionProfile:
    now = repository.utcnow()
    year_list = years or [1860, 1861, 1862, 1863]
    owned_list = owned or []
    return CoinCollectionProfile(
        collection=CoinCollectionSeries(
            id=1,
            collection_key=key,
            name=f"{country} · {ruler} · {denomination}",
            country=country,
            ruler=ruler,
            denomination=denomination,
            size_mm=size_mm,
            size_band=band,
            created_at=now,
            updated_at=now,
        ),
        member_series_ids=[1],
        min_year=year_list[0],
        max_year=year_list[-1],
        years=year_list,
        owned_years=owned_list,
        missing_years=[year for year in year_list if year not in owned_list],
        upgrade_years=upgrade or [],
        year_specs=specs or {year: [CoinYearSpec(year, year, 1, 1000, 0.3, size_mm, 9.4, 10.0, 20.0, "CNY", False)] for year in year_list},
        size_mm=size_mm,
        size_band=band,
    )


# ---------------------------------------------------------------- 清洗与分档


def test_size_band_boundaries():
    assert collection.size_band(None) is None
    assert collection.size_band(24.9) == "小"
    assert collection.size_band(25) == "中"
    assert collection.size_band(29.9) == "中"
    assert collection.size_band(30) == "大"
    assert collection.size_band(34.9) == "大"
    assert collection.size_band(35) == "超大"


def test_composition_filter():
    assert collection.is_bronze_or_copper("Bronze")
    assert collection.is_bronze_or_copper("Copper")
    assert collection.is_bronze_or_copper("青铜")
    assert not collection.is_bronze_or_copper("Copper-nickel")
    assert not collection.is_bronze_or_copper("Silver")
    assert not collection.is_bronze_or_copper(None)


def test_proof_filter_uses_flag_or_description():
    series = _series(1, comments="Proof issues exist")
    assert collection.is_proof_issue(_issue(1, 1, year=1900, is_proof=True), series)
    assert collection.is_proof_issue(_issue(2, 1, year=1901, comment="Proof"), series)
    assert not collection.is_proof_issue(_issue(3, 1, year=1902), series)


def test_year_specs_pick_max_mintage_and_exclude_proof():
    series = _series(1)
    issues = [
        _issue(11, 1, year=1900, mintage=100),
        _issue(12, 1, year=1900, mintage=300, comment="币面有戳记"),
        _issue(13, 1, year=1900, mintage=9999, is_proof=True),
        _issue(14, 1, year=1901, mintage=None),
    ]
    values = [
        _values(12, 1, frequency=0.6, vf=10.0, xf=30.0),
    ]
    specs = collection.build_year_specs(issues, values, series=series)

    assert list(specs) == [1900, 1901]
    assert specs[1900][0].mintage == 300
    assert specs[1900][0].rarity == 0.006
    assert specs[1900][0].price_vf == 10.0
    assert specs[1900][0].price_xf == 30.0
    assert specs[1900][0].comment == "币面有戳记"
    assert specs[1901][0].mintage is None


# ---------------------------------------------------------------- 画像


def test_build_profile_owned_missing_and_upgrade():
    series = _series(1)
    profile = collection.build_profile(
        _collection(),
        [series],
        issues_by_series={
            1: [
                _issue(11, 1, year=1860, mintage=1000),
                _issue(12, 1, year=1861, mintage=800),
                _issue(13, 1, year=1862, mintage=600),
                _issue(14, 1, year=1863, mintage=400),
                _issue(15, 1, year=1864, mintage=200),
            ]
        },
        values_by_series={1: [_values(12, 1, frequency=0.6, vf=10.0, xf=30.0)]},
        orders=[
            CoinOrder("1", None, "英国1860年1便士", 10.0, country="英国", era="1860年", denomination="1便士"),
            CoinOrder("2", None, "英国1862年1便士", 10.0, country="英国", era="1862", denomination="1 便士"),
        ],
        grades=[
            CoinPurchaseGrade(None, "英国", "1861", "1 便士", "差", repository.utcnow()),
            CoinPurchaseGrade(None, "英国", "1862", "1 便士", "好", repository.utcnow()),
        ],
    )

    assert profile.years == [1860, 1861, 1862, 1863, 1864]
    assert profile.owned_years == [1860, 1861, 1862]
    assert profile.missing_years == [1863, 1864]
    assert profile.upgrade_years == [1861]  # 只有「差」需要升级
    assert profile.size_band == "大"


def test_build_profile_excludes_non_bronze_and_proof_years():
    bronze = _series(1)
    silver = _series(2, composition="Silver", size_mm=31.0)
    profile = collection.build_profile(
        _collection(),
        [bronze, silver],
        issues_by_series={
            1: [_issue(11, 1, year=1860), _issue(12, 1, year=1861, is_proof=True)],
            2: [_issue(21, 2, year=1860, mintage=999999)],
        },
        values_by_series={},
        orders=[],
        grades=[],
    )

    assert profile.years == [1860]  # 银币年份与精制年份都被剔除
    assert profile.year_specs[1860][0].mintage == 1000


# ---------------------------------------------------------------- 推荐


def test_priority_one_uses_position_and_scarcity():
    rare = CoinYearSpec(1863, 1863, 1, 100, 0.006, 31.0, 9.4, 10.0, 30.0, "CNY", False)
    common = CoinYearSpec(1864, 1864, 2, 1000, 0.30, 31.0, 9.4, 10.0, 20.0, "CNY", False)
    profile = _profile(
        years=[1860, 1861, 1862, 1863, 1864],
        owned=[1860, 1861, 1862],
        specs={1863: [rare], 1864: [common]},
    )

    items = recommend.build_recommendations([profile], min_results=10)
    first = [item for item in items if item.priority == 1]

    # 1864 是末年（1.5）× 1/0.30 = 5；1863 是稀缺年（1/0.006 = 166.7）
    assert first[0].year == 1863
    assert first[0].score == pytest.approx(1 / 0.006)
    assert first[1].year == 1864
    assert first[1].score == pytest.approx(1.5 / 0.30)


def test_priority_two_only_bad_grade_and_top_three():
    profile = _profile(
        years=[1860, 1861, 1862, 1863, 1864, 1865],
        owned=[1860, 1861, 1862, 1863, 1864],
        upgrade=[1860, 1861, 1862, 1863, 1864],
        specs={
            year: [
                CoinYearSpec(
                    year, year, year, 1000, 0.01, 31.0, 9.4, 10.0, 10.0 + year % 10, "CNY", False
                )
            ]
            for year in [1860, 1861, 1862, 1863, 1864]
        },
    )

    items = recommend.build_recommendations([profile], min_results=10)
    upgrades = [item for item in items if item.priority == 2]

    assert len(upgrades) == 3
    assert all(item.year in profile.upgrade_years for item in upgrades)


def test_japanese_year_labels_translate_back():
    profile = _profile(
        key="jp-meiji-2sen",
        country="日本",
        ruler="明治",
        denomination="2 钱",
        size_mm=31.0,
        years=[6, 7],
        owned=[],
        specs={
            6: [CoinYearSpec(6, 1873, 1, 1000, 0.006, 31.0, 9.4, 10.0, 30.0, "CNY", False)],
            7: [CoinYearSpec(7, 1874, 2, 1000, 0.1, 31.0, 9.4, 10.0, 30.0, "CNY", False)],
        },
    )

    labels = recommend.year_labels(profile, 6, profile.year_specs[6][0])

    assert "明治6年" in labels
    assert "明治六年" in labels
    assert "1873" in labels
    item = recommend.build_recommendations([profile], min_results=10)[0]
    assert item.keyword.startswith("日本 明治6年")


def test_priority_four_only_when_first_two_are_insufficient():
    primary = _profile(
        key="uk-victoria-penny",
        years=[1860, 1861, 1862],
        owned=[1860, 1861],
        specs={
            1860: [CoinYearSpec(1860, 1860, 1, 1000, 0.3, 31.0, 9.4, 10.0, 20.0, "CNY", False)],
            1861: [CoinYearSpec(1861, 1861, 2, 1000, 0.3, 31.0, 9.4, 10.0, 20.0, "CNY", False)],
            1862: [CoinYearSpec(1862, 1862, 3, 1000, 0.3, 31.0, 9.4, 10.0, 20.0, "CNY", False)],
        },
    )
    similar = _profile(
        key="uk-victoria-halfpenny",
        denomination="半 便士",
        size_mm=30.5,
        band="大",
        years=[1880, 1881, 1882],
        owned=[1880],
        specs={
            1880: [CoinYearSpec(1880, 1880, 4, 1000, 0.2, 30.5, 4.5, 10.0, 20.0, "CNY", False)],
            1881: [CoinYearSpec(1881, 1881, 5, 1000, 0.2, 30.5, 4.5, 10.0, 20.0, "CNY", False)],
            1882: [CoinYearSpec(1882, 1882, 6, 1000, 0.2, 30.5, 4.5, 10.0, 20.0, "CNY", False)],
        },
    )

    items = recommend.build_recommendations(
        [primary], explore_profiles=[similar], min_results=3
    )

    # 前两层只有 1 条 → 触发相似推荐；每个系列一条（无拍品可补多条）
    assert [item.priority for item in items] == [1, 4]


def test_priority_four_tops_up_when_items_enough_but_matches_short():
    primary = _profile(
        key="uk-victoria-penny",
        years=[1860, 1861, 1862, 1863, 1864, 1865],
        owned=[],
        specs={
            year: [CoinYearSpec(year, year, 1, 1000, 0.3, 31.0, 9.4, 10.0, 20.0, "CNY", False)]
            for year in [1860, 1861, 1862, 1863, 1864, 1865]
        },
    )
    similar = _profile(
        key="uk-victoria-halfpenny",
        denomination="半 便士",
        size_mm=30.5,
        years=[1880, 1881, 1882, 1883],
        owned=[1880],
    )

    items = recommend.build_recommendations(
        [primary], explore_profiles=[similar], search_results=0, min_results=3
    )

    # 前两层已有 6 条推荐但当日命中 0 条 → 相似推荐补位；每个系列一条
    fours = [item for item in items if item.priority == 4]
    assert len(fours) == 1
    assert len([item for item in items if item.priority == 1]) == 6


def test_priority_four_caps_at_min_results_across_series():
    """给了当日拍品时，多个候选系列各出一条，最多 min_results 个。"""
    primary = _profile(key="uk-victoria-penny", years=[1860], owned=[])
    similars = [
        _profile(
            key=f"uk-victoria-similar-{index}",
            denomination=f"{index} 便士",
            size_mm=30.5,
            years=[1879 + index],
            owned=[],
        )
        for index in range(1, 7)
    ]
    lots = [
        CoinTodayLot(
            id=index,
            session_id=1,
            title=f"英国 {1879 + index} 年 {index} 便士",
            start_price=None,
            current_price=None,
            image_urls=[],
            shop_name=None,
            session_title="",
            auction_at=None,
        )
        for index in range(1, 7)
    ]

    items = recommend.build_recommendations(
        [primary],
        explore_profiles=similars,
        search_results=0,
        similar_lots=lots,
        min_results=5,
    )

    fours = [item for item in items if item.priority == 4]
    assert len(fours) == 5
    assert {item.year for item in fours} == {1880, 1881, 1882, 1883, 1884}


def test_similar_tops_up_same_series_with_multiple_lots():
    """第二轮把同系列拍品追加到同一张卡，不新开卡。"""
    primary = _profile(key="uk-victoria-penny", years=[1860], owned=[])
    similar = _profile(
        key="uk-victoria-halfpenny",
        denomination="半 便士",
        size_mm=30.5,
        years=[1900, 1901, 1902, 1903, 1904],
        owned=[],
    )
    lots = [
        CoinTodayLot(index, 1, f"英国 {1939 + index} 年 半 便士", None, None, [], None, "", None)
        for index in range(1, 6)
    ]

    items = recommend.build_recommendations(
        [primary],
        explore_profiles=[similar],
        search_results=0,
        similar_lots=lots,
        min_results=5,
    )
    recommend.attach_lots(items, lots)
    recommend._prune_similar_lots(items, set())

    fives = [item for item in items if item.priority == 5]
    assert len(fives) == 1
    assert len(fives[0].matched_lots) == 5
    # 面值探索的搜索词不带年份
    assert fives[0].keyword == "英国 半 便士"


def test_similar_excludes_precise_lots_and_reuses_no_lot():
    """相似推荐不碰精准命中的拍品；多个系列只命中同一条拍品时只保留一条。"""
    primary = _profile(key="uk-victoria-penny", years=[1860], owned=[])
    same_lot_series = [
        _profile(
            key=f"uk-victoria-halfpenny-{index}",
            denomination="半 便士",
            size_mm=30.5,
            years=[1880],
            owned=[],
        )
        for index in range(2)
    ]
    # 这个系列只能靠 P5 命中 1860 那条精准拍品，应当被排除
    precise_only = _profile(
        key="uk-victoria-penny-similar",
        denomination="1 便士",
        size_mm=31.0,
        years=[1900],
        owned=[],
    )
    precise_lot = CoinTodayLot(1, 1, "英国 1860 年 1 便士", None, None, [], None, "", None)
    shared_lot = CoinTodayLot(2, 1, "英国 1880 年 半 便士", None, None, [], None, "", None)

    items = recommend.build_recommendations(
        [primary],
        explore_profiles=[*same_lot_series, precise_only],
        search_results=1,
        similar_lots=[precise_lot, shared_lot],
        exclude_lot_ids={1},
        min_results=5,
    )

    # 1860 已由精准匹配占用 → 相似推荐不再出卡；两个系列抢同一条拍品 → 只留一条
    assert [item.priority for item in items if item.priority in (4, 5)] == [4]


def test_similar_card_keeps_single_lot():
    """相似推荐一张卡只挂一条拍品。"""
    primary = _profile(key="uk-victoria-penny", years=[1860], owned=[])
    similar = _profile(
        key="uk-victoria-halfpenny",
        denomination="半 便士",
        size_mm=30.5,
        years=[1880],
        owned=[],
    )
    lots = [
        CoinTodayLot(index, 1, "英国 1880 年 半 便士", None, None, [], None, "", None)
        for index in (2, 3, 4)
    ]

    items = recommend.build_recommendations(
        [primary],
        explore_profiles=[similar],
        search_results=0,
        similar_lots=lots,
        min_results=5,
    )
    recommend.attach_lots(items, lots)
    recommend._prune_similar_lots(items, set())

    fours = [item for item in items if item.priority == 4]
    assert len(fours) == 1
    assert [lot.id for lot in fours[0].matched_lots] == [2]


def test_priority_five_denomination_explore_fills_when_year_misses():
    """P4 年份对不上时，P5 用「同国家 + 同面值」补位，同一系列只出一条。"""
    primary = _profile(key="uk-victoria-penny", years=[1860, 1861], owned=[])
    similar = _profile(
        key="uk-victoria-halfpenny",
        denomination="半 便士",
        size_mm=30.5,
        years=[1900, 1901, 1902],
        owned=[],
    )
    lot = CoinTodayLot(
        id=1,
        session_id=1,
        title="英国 1940 年 半 便士",
        start_price=None,
        current_price=None,
        image_urls=[],
        shop_name=None,
        session_title="",
        auction_at=None,
    )

    items = recommend.build_recommendations(
        [primary],
        explore_profiles=[similar],
        search_results=0,
        similar_lots=[lot],
        min_results=4,
    )

    fives = [item for item in items if item.priority == 5]
    assert len(fives) == 1
    assert fives[0].reason == "面值探索"
    assert recommend.lot_matches(fives[0], lot)
    assert not recommend.lot_matches(items[0], lot)  # P1 仍然要求年份命中


def test_lot_matching_handles_arabic_and_chinese_year_forms():
    profile = _profile(
        key="jp-meiji-2sen",
        country="日本",
        ruler="明治",
        denomination="2 钱",
        years=[6],
        owned=[],
        specs={6: [CoinYearSpec(6, 1873, 1, 1000, 0.006, 31.0, 9.4, 10.0, 30.0, "CNY", False)]},
    )
    item = recommend.build_recommendations([profile], min_results=10)[0]

    def lot(title: str) -> CoinTodayLot:
        return CoinTodayLot(1, 1, title, 0.0, 10.0, [], "店", "专场", None)

    assert recommend.lot_matches(item, lot("日本明治六年二钱"))
    assert recommend.lot_matches(item, lot("日本 明治6年 2钱"))
    assert not recommend.lot_matches(item, lot("日本明治七年二钱"))


# ---------------------------------------------------------------- 映射表读写


def test_collection_series_crud_and_member_mapping(temp_db):
    collection_id = repository.upsert_coin_collection_series(
        collection_key="uk-victoria-penny",
        name="英国 · 维多利亚 · 1 便士",
        country="英国",
        ruler="维多利亚",
        denomination="1 便士",
        size_mm=31.0,
        size_band="大",
    )
    repository.upsert_coin_collection_member(collection_id, 670, "英国 · 维多利亚 · 1 便士")
    repository.upsert_coin_collection_member(collection_id, 671, "英国 · 维多利亚 · 1 便士")

    assert repository.get_coin_collection_series(collection_id).collection_key == "uk-victoria-penny"
    members = repository.list_coin_collection_members(collection_id)
    assert [member.numista_series_id for member in members] == [670, 671]

    assert repository.delete_coin_collection_member(members[0].id)
    assert len(repository.list_coin_collection_members(collection_id)) == 1
    assert repository.delete_coin_collection_series(collection_id)
    assert repository.list_coin_collection_members(collection_id) == []


# ---------------------------------------------------------------- 页面 smoke


@pytest.fixture()
def web(tmp_path, monkeypatch, no_scheduler):
    monkeypatch.setenv("DESKHUB_DB_PATH", str(tmp_path / "coins.db"))
    config.reset()
    with TestClient(fastapi_app) as client:
        yield client
    config.reset()


def test_collection_pages_create_map_and_render(web):
    series = _series(670)
    series_id = repository.upsert_coin_series_cn(series)
    repository.upsert_coin_issue_cn(_issue(9001, series_id, year=1860, mintage=1000, comment="币面有戳记"))
    repository.upsert_coin_issue_cn(_issue(9002, series_id, year=1861, mintage=800))
    repository.upsert_coin_issue_values(_values(9001, 670, frequency=0.6, vf=10.0, xf=30.0))
    repository.upsert_coin_purchase_grade("英国", "1860", "1 便士", "差")
    # 同国家 / 面值的另一个候选，用于验证候选下拉里的年份范围
    repository.upsert_coin_series_cn(
        _series(671, ruler="爱德华七世", min_year=1874, max_year=1901)
    )

    response = web.post(
        "/coins/collections/add",
        data={
            "country": "英国",
            "ruler": "维多利亚",
            "denomination": "1 便士",
            "size_mm": "31",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    collection_row = repository.list_coin_collection_series()[0]

    response = web.post(
        "/coins/collections/members/add",
        data={"collection_id": collection_row.id, "numista_series_id": 670},
        follow_redirects=False,
    )
    assert response.status_code == 302

    page = web.get("/coins/collections")
    assert page.status_code == 200
    assert "英国 · 维多利亚 · 1 便士" in page.text
    assert "670" in page.text
    assert "1860–1864" in page.text  # 成员年份范围
    assert '<option value="维多利亚" selected>' in page.text  # 执政者默认取收藏系列
    assert "1874–1901" not in page.text  # 爱德华七世不在默认执政者下

    page = web.get(f"/coins/collections?expand={collection_row.id}&mruler=__all__")
    assert "1874–1901" in page.text  # 切到「全部」后出现候选年份范围

    page = web.get("/coins/collection")
    assert page.status_code == 200
    assert "今日推荐" in page.text
    assert "收藏画像摘要" in page.text
    assert "英国 · 维多利亚 · 1 便士" in page.text
    assert "1860" in page.text
    assert "币面有戳记" in page.text  # 年份规格字典的备注列
    assert 'class="row-year-upgrade"' in page.text  # 需升级年份行
    assert 'class="row-year-missing"' in page.text  # 缺失年份行


def test_collection_member_filter_by_country_and_denomination(web):
    """候选下拉默认取收藏系列的国家 / 面值，也支持按国家 + 面值筛选。"""
    collection_id = repository.upsert_coin_collection_series(
        collection_key="uk-victoria-penny",
        name="英国 · 维多利亚 · 1 便士",
        country="英国",
        ruler="维多利亚",
        denomination="1 便士",
        size_mm=31.0,
        size_band="大",
    )
    repository.upsert_coin_series_cn(_series(670))
    repository.upsert_coin_series_cn(
        _series(2317, issuer="日本", ruler="明治", denomination="2 钱", min_year=1873, max_year=1892)
    )

    page = web.get(
        f"/coins/collections?expand={collection_id}&mcountry=日本&mdenomination=2 钱"
    )
    assert page.status_code == 200
    assert '<option value="日本" selected>' in page.text
    assert '<option value="2 钱" selected>' in page.text
    assert 'value="2317"' in page.text
    assert "1873–1892" in page.text
    # 英国候选不应出现在日本 / 2 钱筛选结果里
    assert 'value="670"' not in page.text


def test_collection_member_filter_by_ruler(web):
    """候选默认取收藏系列的执政者，也可以切到「全部」查看其他执政者。"""
    collection_id = repository.upsert_coin_collection_series(
        collection_key="uk-penny-ruler-test",
        name="英国 · 维多利亚 · 1 便士",
        country="英国",
        ruler="维多利亚",
        denomination="1 便士",
        size_mm=31.0,
        size_band="大",
    )
    repository.upsert_coin_series_cn(_series(670))
    repository.upsert_coin_series_cn(
        _series(671, ruler="爱德华七世", min_year=1874, max_year=1901)
    )

    page = web.get(f"/coins/collections?expand={collection_id}")
    assert '<option value="维多利亚" selected>' in page.text
    assert 'value="670"' in page.text
    assert 'value="671"' not in page.text

    page = web.get(f"/coins/collections?expand={collection_id}&mruler=__all__")
    assert '<option value="__all__" selected>' in page.text
    assert 'value="671"' in page.text


def test_collections_page_paginates(web):
    for index in range(21):
        repository.upsert_coin_collection_series(
            collection_key=f"c{index:02d}",
            name=f"国{index:02d} · 测试 · 1 便士",
            country=f"国{index:02d}",
            ruler="测试",
            denomination="1 便士",
            size_mm=30.0,
            size_band="大",
        )

    page = web.get("/coins/collections")
    assert "第 1 / 2 页" in page.text
    assert "国00" in page.text
    assert "国20" not in page.text

    page = web.get("/coins/collections?page=2")
    assert "第 2 / 2 页" in page.text
    assert "国20" in page.text


def test_collection_page_paginates_recommendations(web):
    series_id = repository.upsert_coin_series_cn(
        _series(900, min_year=1800, max_year=1824)
    )
    for year in range(1800, 1825):
        repository.upsert_coin_issue_cn(_issue(10000 + year, series_id, year=year))
    collection_id = repository.upsert_coin_collection_series(
        collection_key="uk-victoria-penny-page",
        name="英国 · 维多利亚 · 1 便士",
        country="英国",
        ruler="维多利亚",
        denomination="1 便士",
        size_mm=31.0,
        size_band="大",
    )
    repository.upsert_coin_collection_member(collection_id, 900, "英国 · 维多利亚 · 1 便士")

    page = web.get("/coins/collection?hit=all")
    assert page.status_code == 200
    assert "25 条" in page.text
    assert "第 1 / 2 页" in page.text

    page = web.get("/coins/collection?hit=all&page=2")
    assert page.status_code == 200
    assert "第 2 / 2 页" in page.text


def test_collection_page_filters_series_reason_and_hit(web):
    """推荐表支持按系列 / 推荐原因 / 是否命中筛选。"""
    series_id = repository.upsert_coin_series_cn(
        _series(901, min_year=1800, max_year=1804)
    )
    for year in range(1800, 1805):
        repository.upsert_coin_issue_cn(_issue(20000 + year, series_id, year=year))
    collection_id = repository.upsert_coin_collection_series(
        collection_key="uk-penny-filter",
        name="英国 · 维多利亚 · 1 便士",
        country="英国",
        ruler="维多利亚",
        denomination="1 便士",
        size_mm=31.0,
        size_band="大",
    )
    repository.upsert_coin_collection_member(collection_id, 901, "英国 · 维多利亚 · 1 便士")

    session_id = repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id="today-session",
            shop_id=None,
            title="今日专场",
            status="预展",
            auction_at=repository.utcnow(),
            auction_at_raw=None,
            deposit=None,
            lot_total=1,
            view_count=0,
            cover_urls=[],
            fetched_at=repository.utcnow(),
        )
    )
    repository.upsert_coin_lot(
        CoinLot(
            id=None,
            session_id=session_id,
            mxiqi_id="today-lot",
            lot_no="1",
            title="英国 1800 年 1 便士",
            start_price=1.0,
            current_price=2.0,
            image_urls=[],
            tags={},
            match_score=0.0,
            is_recommended=False,
            fetched_at=repository.utcnow(),
        )
    )
    other_session_id = repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id="other-session",
            shop_id=None,
            title="无关专场",
            status="预展",
            auction_at=repository.utcnow(),
            auction_at_raw=None,
            deposit=None,
            lot_total=1,
            view_count=0,
            cover_urls=[],
            fetched_at=repository.utcnow(),
        )
    )
    repository.upsert_coin_lot(
        CoinLot(
            id=None,
            session_id=other_session_id,
            mxiqi_id="other-lot",
            lot_no="1",
            title="某无关纪念章",
            start_price=1.0,
            current_price=2.0,
            image_urls=[],
            tags={},
            match_score=0.0,
            is_recommended=False,
            fetched_at=repository.utcnow(),
        )
    )

    page = web.get("/coins/collection?series=uk-penny-filter&reason=1&hit=1")
    assert page.status_code == 200
    assert "1 条推荐" in page.text
    assert 'data-year="1800"' in page.text
    assert 'data-year="1801"' not in page.text
    # 今日推荐页要直接展示命中的店铺 / 拍品，而不是只给条数
    assert "英国 1800 年 1 便士" in page.text
    assert "今日拍场" in page.text
    # 顶部指标只统计精准匹配；右侧聚合面板用高亮圆圈显示精准匹配件数
    assert "精准匹配店铺" in page.text
    assert "精准匹配拍品" in page.text
    assert 'coin-rail-circle is-precise">1 件<' in page.text
    # 圆圈在右侧居中，统计顺序为总件数 → 精准 → 相似
    assert page.text.index("coin-rail-body") < page.text.index("coin-rail-circle")
    assert "1 件 · 精准 1 · 相似 0" in page.text
    # 未匹配场次折叠为「其他专场」，查看按钮跳当日全部拍场
    assert "其他专场" in page.text
    assert "1 场 · 0 件命中" in page.text
    assert 'href="/coins?auction_date=' in page.text and "rec=all" in page.text
    # 推荐模块标题按「国家 · 执政者 · 面值」；下拉不再有相似推荐
    assert "<h3>英国 · 维多利亚 · 1 便士</h3>" in page.text
    assert '<option value="3"' not in page.text

    page = web.get("/coins/collection?hit=0")
    # 「未命中」只看相似推荐，精准匹配整段隐藏
    assert 'data-year="1800"' not in page.text
    assert 'data-year="1801"' not in page.text
    assert "暂无相似推荐" in page.text

    # 「是否命中」默认已命中；显式选「全部」才不过滤
    page = web.get("/coins/collection?series=uk-penny-filter")
    assert '<option value="1" selected>已命中' in page.text
    assert 'data-year="1800"' in page.text
    assert 'data-year="1801"' not in page.text

    page = web.get("/coins/collection?series=uk-penny-filter&hit=all")
    assert '<option value="all" selected>全部</option>' in page.text
    assert 'data-year="1800"' in page.text
    assert 'data-year="1801"' in page.text

    page = web.get("/coins/collection?reason=2")
    assert "暂无推荐项" in page.text


def test_collection_hit_filter_separates_precise_and_similar(web):
    """相似推荐（P4/P5）不计入命中；无论是否命中，相似推荐模块都常驻展示。"""
    series_id = repository.upsert_coin_series_cn(
        _series(902, min_year=1800, max_year=1804)
    )
    for year in range(1800, 1805):
        repository.upsert_coin_issue_cn(_issue(11000 + year, series_id, year=year))
    collection_id = repository.upsert_coin_collection_series(
        collection_key="uk-penny-similar",
        name="英国 · 维多利亚 · 1 便士",
        country="英国",
        ruler="维多利亚",
        denomination="1 便士",
        size_mm=31.0,
        size_band="大",
    )
    repository.upsert_coin_collection_member(collection_id, 902, "英国 · 维多利亚 · 1 便士")
    repository.upsert_coin_explore_candidate(_explore_candidate(990007))
    session_id = repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id="similar-session",
            shop_id=None,
            title="法国硬币专场",
            status="预展",
            auction_at=repository.utcnow(),
            auction_at_raw=None,
            deposit=None,
            lot_total=1,
            view_count=0,
            cover_urls=[],
            fetched_at=repository.utcnow(),
        )
    )
    repository.upsert_coin_lot(
        CoinLot(
            id=None,
            session_id=session_id,
            mxiqi_id="similar-lot",
            lot_no="1",
            title="法国1900年1便士",
            start_price=1.0,
            current_price=2.0,
            image_urls=[],
            tags={},
            match_score=0.0,
            is_recommended=False,
            fetched_at=repository.utcnow(),
        )
    )

    page = web.get("/coins/collection?hit=1")
    assert "今日暂无收藏系列命中" in page.text
    # 只展示当日有拍品命中的相似目标；1901 没有拍品，不再出现
    assert 'data-year="1900"' in page.text
    assert 'data-year="1901"' not in page.text

    page = web.get("/coins/collection?hit=0")
    assert 'data-year="1900"' in page.text
    assert 'data-year="1901"' not in page.text


def test_collection_rail_counts_similar_matches_with_transliteration(web):
    """站点写「雷普塔」、推荐词写「莱普塔」时也要匹配，并计入右侧相似角标。"""
    series_id = repository.upsert_coin_series_cn(
        _series(904, min_year=1800, max_year=1804)
    )
    for year in range(1800, 1805):
        repository.upsert_coin_issue_cn(_issue(13000 + year, series_id, year=year))
    collection_id = repository.upsert_coin_collection_series(
        collection_key="uk-penny-similar-rail",
        name="英国 · 维多利亚 · 1 便士",
        country="英国",
        ruler="维多利亚",
        denomination="1 便士",
        size_mm=31.0,
        size_band="大",
    )
    repository.upsert_coin_collection_member(collection_id, 904, "英国 · 维多利亚 · 1 便士")
    repository.upsert_coin_explore_candidate(
        _explore_candidate(
            990009,
            issuer="希腊",
            ruler="乔治一世",
            denomination="10 莱普塔",
            title="希腊 · 10 莱普塔",
            years=(1869, 1870),
        )
    )
    session_id = repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id="similar-rail-session",
            shop_id=None,
            title="希腊专场",
            status="预展",
            auction_at=repository.utcnow(),
            auction_at_raw=None,
            deposit=None,
            lot_total=1,
            view_count=0,
            cover_urls=[],
            fetched_at=repository.utcnow(),
        )
    )
    repository.upsert_coin_lot(
        CoinLot(
            id=None,
            session_id=session_id,
            mxiqi_id="similar-rail-lot",
            lot_no="1",
            title="希腊王国1869年10雷普塔(乔治一世第一版)",
            start_price=1.0,
            current_price=2.0,
            image_urls=[],
            tags={},
            match_score=0.0,
            is_recommended=False,
            fetched_at=repository.utcnow(),
        )
    )

    page = web.get("/coins/collection")
    assert "希腊王国1869年10雷普塔" in page.text
    assert 'coin-rail-circle is-similar">1 件<' in page.text
    # 只展示当日有拍品命中的相似目标：1 项 1 件
    assert "相似 1 项 · 命中 1 件" in page.text


def test_sessions_page_rematches_today_before_listing(web):
    """拍场页在没有命中专场时，按当前画像重算当日匹配后再展示。"""
    series_id = repository.upsert_coin_series_cn(
        _series(903, min_year=1800, max_year=1804)
    )
    for year in range(1800, 1805):
        repository.upsert_coin_issue_cn(_issue(12000 + year, series_id, year=year))
    collection_id = repository.upsert_coin_collection_series(
        collection_key="uk-penny-rematch",
        name="英国 · 维多利亚 · 1 便士",
        country="英国",
        ruler="维多利亚",
        denomination="1 便士",
        size_mm=31.0,
        size_band="大",
    )
    repository.upsert_coin_collection_member(collection_id, 903, "英国 · 维多利亚 · 1 便士")

    session_id = repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id="rematch-session",
            shop_id=None,
            title="今日专场",
            status="预展",
            auction_at=repository.utcnow(),
            auction_at_raw=None,
            deposit=None,
            lot_total=1,
            view_count=0,
            cover_urls=[],
            fetched_at=repository.utcnow(),
        )
    )
    repository.upsert_coin_lot(
        CoinLot(
            id=None,
            session_id=session_id,
            mxiqi_id="rematch-lot",
            lot_no="1",
            title="英国 1800 年 1 便士",
            start_price=1.0,
            current_price=2.0,
            image_urls=[],
            tags={},
            match_score=0.0,
            is_recommended=False,
            fetched_at=repository.utcnow(),
        )
    )

    page = web.get("/coins")
    assert f'href="/coins/session/{session_id}"' in page.text


def test_web_fixture_uses_empty_db(web):
    """页面在空库下也要 200（没有收藏系列时给空态）。"""
    assert web.get("/coins/collection").status_code == 200
    assert web.get("/coins/collections").status_code == 200
    assert web.get("/coins/explore").status_code == 200


# ---------------------------------------------------------------- 探索候选池（spec §14.17.6）


def _explore_candidate(
    numista_id: int,
    *,
    composition: str = "青铜",
    size_mm: float = 31.0,
    years: tuple[int, ...] = (1900, 1901),
    title: str = "法国 · 10 分",
    issuer: str = "法国",
    ruler: str = "第三共和国",
    denomination: str = "1 便士",
) -> CoinExploreCandidate:
    now = repository.utcnow()
    payload = {
        "numista_id": numista_id,
        "title": title,
        "url": None,
        "object_type": "标准流通币",
        "issuer": issuer,
        "min_year": years[0],
        "max_year": years[-1],
        "ruler": ruler,
        "denomination": denomination,
        "size_mm": size_mm,
        "weight_g": 10.0,
        "thickness_mm": None,
        "shape": "圆形",
        "composition": composition,
        "edge": None,
        "obverse_desc": None,
        "obverse_lettering": None,
        "obverse_picture": None,
        "reverse_desc": None,
        "reverse_lettering": None,
        "reverse_picture": None,
        "catalog_no": None,
        "comments": None,
        "mints": [],
    }
    issues = [
        {
            "numista_id": numista_id * 100 + index,
            "series_numista_id": numista_id,
            "year": year,
            "gregorian_year": year,
            "mint": None,
            "mint_mark": None,
            "variety": None,
            "comment": None,
            "mintage": 1000,
            "size_mm": size_mm,
            "weight_g": 10.0,
            "thickness_mm": None,
            "edge": None,
            "is_proof": False,
            "is_essai": False,
            "picture": None,
            "catalog_no": None,
        }
        for index, year in enumerate(years)
    ]
    return CoinExploreCandidate(
        id=None,
        numista_id=numista_id,
        seed_key="uk-victoria-penny",
        seed_name="英国 · 维多利亚 · 1 便士",
        size_diff=0.0,
        status="candidate",
        payload=payload,
        payload_cn=payload,
        issues=issues,
        verified_at=now,
        created_at=now,
        updated_at=now,
    )


def test_explore_candidate_status_ignored_is_hidden_and_not_resurrected(temp_db):
    repository.upsert_coin_explore_candidate(_explore_candidate(990001))
    assert [item.numista_id for item in repository.list_coin_explore_candidates()] == [990001]

    assert repository.set_coin_explore_candidate_status(990001, "ignored")
    assert repository.list_coin_explore_candidates() == []
    assert repository.list_coin_explore_candidates(include_ignored=True)[0].status == "ignored"

    # 重新扫描不应把忽略过的候选复活
    repository.upsert_coin_explore_candidate(_explore_candidate(990001))
    assert repository.list_coin_explore_candidates() == []


def test_build_seeds_uses_profile_size_when_collection_size_missing():
    """映射行没填 size_mm 时，种子要用画像从成员系列算出的尺寸（否则永远 0 个种子）。"""
    profile = _profile(size_mm=31.0)
    profile = replace(profile, collection=replace(profile.collection, size_mm=None))

    seeds = explore.build_seeds([profile])

    assert [(seed.name, seed.size_mm) for seed in seeds] == [
        ("英国 · 维多利亚 · 1 便士", 31.0)
    ]


def test_match_seed_requires_copper_and_size_tolerance():
    seed = explore.ExploreSeed(key="k", name="n", query="penny", size_mm=31.0)
    info = SeriesInfo(
        numista_id=1,
        title=None,
        url=None,
        object_type=None,
        issuer=None,
        min_year=None,
        max_year=None,
        ruler=None,
        denomination=None,
        size_mm=32.5,
        weight_g=None,
        thickness_mm=None,
        shape=None,
        composition="Bronze",
        edge=None,
        obverse_desc=None,
        obverse_lettering=None,
        obverse_picture=None,
        reverse_desc=None,
        reverse_lettering=None,
        reverse_picture=None,
        catalog_no=None,
        comments=None,
        mints=[],
    )
    matched = explore.match_seed(info, [seed], 2.0)
    assert matched is not None and matched[1] == pytest.approx(1.5)
    assert explore.match_seed(info, [seed], 1.0) is None
    assert (
        explore.match_seed(
            SeriesInfo(**{**info.__dict__, "composition": "Copper-nickel"}), [seed], 5.0
        )
        is None
    )


def test_explore_profiles_feed_similar_recommendations(temp_db):
    repository.upsert_coin_explore_candidate(_explore_candidate(990002))
    candidate_profiles = collection.load_explore_profiles()
    assert len(candidate_profiles) == 1
    assert candidate_profiles[0].missing_years == [1900, 1901]

    primary = _profile(years=[1860], owned=[])
    items = recommend.build_recommendations(
        [primary],
        explore_profiles=candidate_profiles,
        search_results=0,
        min_results=5,
    )
    assert any(item.priority in (4, 5) for item in items)


def test_explore_page_adopt_and_ignore(web):
    repository.upsert_coin_explore_candidate(_explore_candidate(990003))
    repository.upsert_coin_explore_candidate(
        _explore_candidate(990004, title="法国 · 20 分")
    )

    page = web.get("/coins/explore")
    assert page.status_code == 200
    assert "990003" in page.text and "990004" in page.text
    assert "来源种子" not in page.text and "系列名称" not in page.text
    assert 'href="/coins/series/990003"' in page.text
    assert "法国" in page.text and "第三共和国" in page.text and "1 便士" in page.text
    assert "1900" in page.text and "1901" in page.text
    assert "coin-status-option is-active" in page.text
    assert "保存" in page.text

    # 未采纳的候选也能在本地看详情（不跳 Numista 站点）
    detail = web.get("/coins/series/990004")
    assert detail.status_code == 200
    assert "法国" in detail.text and "第三共和国" in detail.text

    response = web.post(
        "/coins/explore/status",
        data={"numista_id": 990003, "status": "adopted"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert repository.get_coin_series(990003, cn=True) is not None
    assert repository.list_coin_issues(repository.get_coin_series(990003).id)

    assert "990003" in web.get("/coins/info").text
    assert "990003" in web.get("/coins/manage").text

    web.post("/coins/explore/status", data={"numista_id": 990004, "status": "ignored"})
    page = web.get("/coins/explore")
    assert "990004" not in page.text


def test_explore_page_filters_by_country_ruler_denomination(web):
    repository.upsert_coin_explore_candidate(_explore_candidate(990005))
    uk = _explore_candidate(990006, title="英国 · 1 便士")
    uk.payload_cn.update(issuer="英国", ruler="维多利亚", denomination="1 便士")
    repository.upsert_coin_explore_candidate(uk)

    page = web.get("/coins/explore")
    assert "990005" in page.text and "990006" in page.text
    assert '<option value="英国"' in page.text and '<option value="法国"' in page.text

    # 选国家后，执政者下拉只剩该国的值，列表也只剩该国候选
    page = web.get("/coins/explore", params={"country": "英国"})
    assert "990006" in page.text and "990005" not in page.text
    assert "第三共和国" not in page.text
    assert '<option value="英国" selected>' in page.text
    assert '<option value="维多利亚">' in page.text

    page = web.get(
        "/coins/explore",
        params={"country": "英国", "ruler": "维多利亚", "denomination": "1 便士"},
    )
    assert "990006" in page.text and "990005" not in page.text
    assert "重置" in page.text

    page = web.get("/coins/explore", params={"country": "法国"})
    assert "990005" in page.text and "990006" not in page.text

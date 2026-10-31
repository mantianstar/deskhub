from __future__ import annotations

from app.coins import recommend
from app.models import (
    CoinCollectionSeries,
    CoinRecommendation,
    CoinTodayLot,
    CoinYearSpec,
)

NOW = "2026-01-01 00:00:00"


def _song_series() -> CoinCollectionSeries:
    return CoinCollectionSeries(
        id=1,
        collection_key="cn-songqingling-100",
        name="中国 · 宋庆龄诞辰100周年 · 1元",
        country="中国",
        ruler=None,
        denomination="1元",
        size_mm=25.0,
        size_band="小",
        created_at=NOW,
        updated_at=NOW,
    )


def _lot(title: str) -> CoinTodayLot:
    return CoinTodayLot(1, 1, title, 0.0, 10.0, [], "店", "专场", None)


def test_similar_recommendation_rejects_unrelated_same_year_lot():
    """P4 相似推荐：纪念主题不同、仅年份巧合（1993）的拍品不应命中宋庆龄系列。"""
    item = CoinRecommendation(
        priority=4,
        collection=_song_series(),
        year=1993,
        year_labels=["1993"],
        keyword="中国 宋庆龄诞辰100周年 纪念币",
        score=0.5,
        reason="年份匹配",
        spec=CoinYearSpec(1993, 1993, 1, 1000, 0.3, 25.0, 9.4, 10.0, 20.0, "CNY", True),
        coin_name="",
        commemorated_topic="宋庆龄诞辰100周年",
    )
    unrelated = _lot("NGC PF67UC 中国1993年精制牡丹1元")
    assert not recommend.lot_matches(item, unrelated)
    assert recommend.lot_matches(item, _lot("中国 1993年 宋庆龄诞辰100周年 纪念币 1元"))


def test_similar_recommendation_p5_still_requires_theme():
    """P5 面值探索不卡年份，但纪念币仍要卡纪念主题。"""
    item = CoinRecommendation(
        priority=5,
        collection=_song_series(),
        year=1993,
        year_labels=["1993"],
        keyword="中国 宋庆龄诞辰100周年 纪念币",
        score=0.5,
        reason="面值探索",
        commemorated_topic="宋庆龄诞辰100周年",
    )
    assert not recommend.lot_matches(item, _lot("NGC PF67UC 中国1993年精制牡丹1元"))
    assert recommend.lot_matches(item, _lot("中国 宋庆龄诞辰100周年 纪念币 1元"))


def test_precise_recommendation_checks_issue_theme():
    """精准匹配的纪念币按「国家 + 发行主题」校验，年份照旧卡。"""
    item = CoinRecommendation(
        priority=1,
        collection=_song_series(),
        year=1993,
        year_labels=["1993"],
        keyword="中国 1993 1元",
        score=0.5,
        reason="缺失年份",
        issue_theme="宋庆龄诞辰100周年",
    )
    assert not recommend.lot_matches(item, _lot("中国1993年精制牡丹1元"))
    assert not recommend.lot_matches(item, _lot("中国 宋庆龄诞辰100周年 纪念币 1992年 1元"))
    assert recommend.lot_matches(item, _lot("中国 宋庆龄诞辰100周年 纪念币 1993年 1元"))


def test_non_commemorative_matching_unchanged():
    """非纪念币（无主题）沿用「国家 + 面值 + 年份」。"""
    item = CoinRecommendation(
        priority=1,
        collection=_song_series(),
        year=1993,
        year_labels=["1993"],
        keyword="中国 1993 1元",
        score=0.5,
        reason="缺失年份",
    )
    assert recommend.lot_matches(item, _lot("中国1993年精制牡丹1元"))


def test_similar_keyword_suffix_and_optional_country():
    series = _song_series()
    assert (
        recommend._similar_keyword(series, "大熊猫", [], "1元") == "中国 大熊猫 纪念币"
    )
    assert (
        recommend._similar_keyword(series, "宋庆龄诞辰100周年纪念币", [], "1元")
        == "中国 宋庆龄诞辰100周年纪念币"
    )
    bare = CoinCollectionSeries(
        id=2,
        collection_key="cn-panda",
        name="大熊猫",
        country="",
        ruler=None,
        denomination="1元",
        size_mm=None,
        size_band=None,
        created_at=NOW,
        updated_at=NOW,
    )
    assert recommend._similar_keyword(bare, "大熊猫", [], "1元") == "大熊猫 纪念币"
    # 面值探索：纪念币品类即使没有主题也要补「纪念币」，非纪念币不变
    assert (
        recommend._similar_keyword(series, "", [], "1元", with_year=False, commemorative=True)
        == "中国 1元 纪念币"
    )
    assert recommend._similar_keyword(series, "", [], "1元", with_year=False) == "中国 1元"

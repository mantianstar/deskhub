"""画像匹配测试（M9-1，plan §18.11）。全程离线，不碰站点也不碰 LLM。"""

from __future__ import annotations

import pytest

from app import config as config_module
from app import db, repository
from app.coins import matcher
from app.models import CoinLot, CoinProfileTag, CoinSession, CoinShop

_FETCHED_AT = "2026-10-09T00:00:00Z"


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


def _tag(dimension: str, value: str, weight: float, *, enabled: bool = True) -> CoinProfileTag:
    return CoinProfileTag(
        id=1,
        dimension=dimension,
        value=value,
        weight=weight,
        hit_count=1,
        enabled=enabled,
        note=None,
        created_at=_FETCHED_AT,
        updated_at=_FETCHED_AT,
    )


# ---------------------------------------------------------------- 归一化


def test_normalize_title_strips_marketing_words_and_space():
    assert matcher.normalize_title("极美品 英国1919年半便士 包老保真") == "英国1919年半便士"
    # 品相/包浆不是营销词，不能被删（它是 condition 维度的值）
    assert "原光" in matcher.normalize_title("原光 1950年英国半便士")


# ---------------------------------------------------------------- 命中


def test_match_lot_hits_by_substring_and_sums_weight():
    tags = [
        _tag("country", "英国", 1.0),
        _tag("denomination", "半便士", 1.0),
        _tag("era", "1959", 0.5),
    ]

    hits = matcher.match_lot("极美品 英国1919年半便士 铜币", tags)

    assert {hit.value for hit in hits} == {"英国", "半便士"}  # 1959 不在标题里 → 不命中
    assert matcher.score_hits(hits) == 2.0


def test_match_lot_ignores_disabled_and_non_matchable_dimensions():
    tags = [
        _tag("country", "英国", 1.0, enabled=False),
        _tag("shop", "猩哥外币小铺", 5.0),  # 不可匹配维度
    ]
    assert matcher.match_lot("英国1919年半便士", tags) == []


def test_match_lot_empty_title_or_no_tags():
    assert matcher.match_lot("   ", [_tag("country", "英国", 1.0)]) == []
    assert matcher.match_lot("英国1919年", []) == []


def test_match_lot_does_not_match_number_inside_longer_number():
    """「5分」不许咬进「25分」—— 实测这会让推荐价查到完全不相干的币（2026-10-12）。"""
    tags = [_tag("denomination", "5分", 1.0), _tag("era", "1961", 1.0)]

    assert matcher.match_lot("美国1962年精制25分 6.25g900银", tags) == []
    assert [hit.value for hit in matcher.match_lot("日本1961年精制5分", tags)] == ["5分", "1961"]
    # 值在数字边界上仍算命中（"1961年" 的 1 后面是「年」，不是数字）
    assert [hit.value for hit in matcher.match_lot("1961年华盛顿25美分", tags)] == ["1961"]


# ---------------------------------------------------------------- 关键词


def test_build_keyword_priority_country_then_era_then_denomination():
    hits = [
        matcher.TagHit("denomination", "半便士", 1.0),
        matcher.TagHit("country", "英国", 0.7),
        matcher.TagHit("era", "1919", 1.0),
    ]
    assert matcher.build_keyword(hits) == "英国 1919 半便士"


def test_build_keyword_single_term():
    assert matcher.build_keyword([matcher.TagHit("category", "铜币", 1.0)]) == "铜币"


# ---------------------------------------------------------------- 重算推荐位


def _seed_lots() -> None:
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
            lot_total=2,
            view_count=0,
            cover_urls=[],
            fetched_at=_FETCHED_AT,
        )
    )
    for mxiqi_id, title in (("l1", "英国1919年半便士"), ("l2", "某无关的徽章")):
        repository.upsert_coin_lot(
            CoinLot(
                id=None,
                session_id=session_id,
                mxiqi_id=mxiqi_id,
                lot_no=None,
                title=title,
                start_price=1.0,
                current_price=0.0,
                image_urls=[],
                tags={},
                match_score=0.0,
                is_recommended=False,
                fetched_at=_FETCHED_AT,
            )
        )


def test_rematch_all_marks_only_matching_lots(temp_db):
    _seed_lots()
    # 权重 1.0 + 1.0 = 2.0 >= 阈值 1.5；无关的那条 0 分
    for dimension, value in (("country", "英国"), ("denomination", "半便士")):
        repository.upsert_coin_tag(dimension, value, 1.0, 1)

    result = matcher.rematch_all(config_module.load())

    assert result.lots_total == 2
    assert result.lots_recommended == 1
    recommended = [lot for lot in repository.list_coin_lots_for_matching() if lot.is_recommended]
    assert [lot.title for lot in recommended] == ["英国1919年半便士"]
    assert recommended[0].match_score == 2.0

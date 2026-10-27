"""`home.filter` 扫描的字段映射与回填（需求 B.5.1）。"""

from __future__ import annotations

import json

import pytest

from app import db, repository
from app.coins import filter_scan
from app.models import CoinLot, CoinSession


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


def _seed_lot(mxiqi_id: str, *, bid_num: int | None = None) -> None:
    session_id = repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id=mxiqi_id.split(":")[0],
            shop_id=None,
            title="测试专场",
            status="running",
            auction_at=None,
            auction_at_raw=None,
            deposit=None,
            lot_total=None,
            view_count=None,
            cover_urls=[],
            fetched_at=repository.utcnow(),
            kind="live",
        )
    )
    repository.upsert_coin_lot(
        CoinLot(
            id=None,
            session_id=session_id,
            mxiqi_id=mxiqi_id,
            lot_no=None,
            title="拍品",
            start_price=0.0,
            current_price=0.0,
            image_urls=[],
            tags={},
            match_score=0.0,
            is_recommended=False,
            fetched_at=repository.utcnow(),
            bid_num=bid_num,
        )
    )


def _lot_row(mxiqi_id: str) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM coin_lots WHERE mxiqi_id = ?", (mxiqi_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


def test_tags_from_attrs_builds_json_object():
    attrs = [
        {"attr_id": 1, "attr_name": "发行年份", "attr_value": "1808"},
        {"attr_id": 2, "attr_name": "国家地区", "attr_value": "印度"},
    ]
    assert json.loads(filter_scan.tags_from_attrs(attrs)) == {
        "发行年份": "1808",
        "国家地区": "印度",
    }
    assert filter_scan.tags_from_attrs([]) is None


def test_bid_users_serializes_uid_array():
    assert filter_scan.bid_users_json([12, 34]) == "[12, 34]"
    assert filter_scan.bid_users_json([]) is None
    assert filter_scan.bid_users_json(None) is None


def test_update_row_keeps_missing_bid_num_empty():
    row = filter_scan.update_row({"auction_id": 329043, "item_id": 7001, "bid_user_num": 3})
    assert row is not None
    assert row["mxiqi_id"] == "329043:7001"
    assert row["bid_num"] is None
    assert row["bid_user_num"] == 3


def test_update_filter_heat_skips_unknown_mxiqi_id(temp_db):
    row = filter_scan.update_row({"auction_id": 1, "item_id": 2, "bid_num": 9})
    assert row is not None
    assert repository.update_coin_lot_filter_heat([row]) == (0, 1)


def test_update_filter_heat_writes_heat_and_winner(temp_db):
    _seed_lot("329043:7001", bid_num=3)
    row = filter_scan.update_row(
        {
            "auction_id": 329043,
            "item_id": 7001,
            "bid_user_num": 5,
            "bid_users": [11, 12],
            "won_user_info": {"uid": 42, "nickname": "买家甲", "nickname_real": "真名"},
            "attrs_aggs": [{"attr_name": "发行年份", "attr_value": "1808"}],
        }
    )
    assert row is not None
    assert repository.update_coin_lot_filter_heat([row]) == (1, 0)

    lot = _lot_row("329043:7001")
    # 缺键的 bid_num 不写 0，也不覆盖已有值
    assert lot["bid_num"] == 3
    assert lot["bid_user_num"] == 5
    assert json.loads(lot["bid_users"]) == [11, 12]
    assert lot["winner_uid"] == "42"
    assert lot["winner_nickname"] == "买家甲"
    assert json.loads(lot["tags"]) == {"发行年份": "1808"}
    assert lot["bid_source"] == "home_filter"

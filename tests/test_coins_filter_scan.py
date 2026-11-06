"""`home.filter` 扫描的字段映射与回填（需求 B.5.1）。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
            fetched_at=repository.now_local(),
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
            fetched_at=repository.now_local(),
            bid_num=bid_num,
        )
    )


def _seed_gap(cate: str = "11010") -> None:
    """造一条已成交但缺出价人数的拍品，让该分类进入深扫。"""
    _seed_lot(f"33{cate}:1")
    with db.connect() as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO coin_categories (code, name, fetched_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (cate, cate, repository.now_local(), repository.now_local()),
        )
        conn.execute(
            "UPDATE coin_lots SET outcome = 'sold', category_code = ? WHERE mxiqi_id = ?",
            (cate, f"33{cate}:1"),
        )


def _set_closed_yesterday(cate: str) -> None:
    """把该分类的成交时间挪到昨天，命中「昨日成交」缺口口径。"""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    with db.connect() as conn, conn:
        conn.execute(
            "UPDATE coin_lots SET closed_at = ? WHERE category_code = ?",
            (yesterday.strftime("%Y-%m-%dT12:00:00"), cate),
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


class _FakeApi:
    """按页返回预设条目，并记录请求过的页码（全量续扫用例）。"""

    def __init__(self, pages: dict[int, list[dict]]) -> None:
        self.pages = pages
        self.requested: list[int] = []

    async def __aenter__(self) -> _FakeApi:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def search_filter_items(self, search_params: dict, page: int) -> list[dict]:
        self.requested.append(page)
        return self.pages.get(page, [])


async def _noop() -> None:
    return None


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(tz=timezone.utc))


def _item(page: int) -> dict:
    return {"auction_id": 1, "item_id": page}


def test_full_scan_resumes_from_saved_page(temp_db, monkeypatch):
    _seed_gap()
    repository.set_coin_filter_scan_page("11010", 30)
    fake = _FakeApi({page: [_item(page)] for page in range(1, 31)})
    monkeypatch.setattr(filter_scan, "MxiqiGapi", lambda cfg: fake)
    monkeypatch.setattr(
        repository, "list_coin_categories", lambda parent: [{"code": "11010"}]
    )
    monkeypatch.setattr(filter_scan, "DAILY_QUOTA", 21)  # 20 页顶部 + 1 页深处
    monkeypatch.setattr(filter_scan, "_pause", _noop)

    result = asyncio.run(filter_scan.scan(_cfg(), full=True))

    assert fake.requested == [*range(1, 21), 30]  # 顶部页先扫完，再从断点续扫深处
    assert result.stopped == "quota"
    assert repository.coin_filter_scan_pages()["11010"] == 31


def test_full_scan_marks_deep_done_when_category_exhausted(temp_db, monkeypatch):
    _seed_gap()
    repository.set_coin_filter_scan_page("11010", 22)
    fake = _FakeApi({page: [_item(page)] for page in range(1, 23)})  # 第 23 页为空 → 已扫完
    monkeypatch.setattr(filter_scan, "MxiqiGapi", lambda cfg: fake)
    monkeypatch.setattr(
        repository, "list_coin_categories", lambda parent: [{"code": "11010"}]
    )
    monkeypatch.setattr(filter_scan, "DAILY_QUOTA", 30)
    monkeypatch.setattr(filter_scan, "_pause", _noop)

    asyncio.run(filter_scan.scan(_cfg(), full=True))

    assert fake.requested == [*range(1, 21), 22, 23]  # 顶部 20 页 + 深处续扫到空页
    # 空页记「已扫到底」而不是重置为 1，避免下一轮又从近期页重扫
    assert repository.coin_filter_scan_pages() == {"11010": filter_scan.DEEP_DONE_PAGE}


def test_full_scan_restarts_done_category_with_gap(temp_db, monkeypatch):
    _seed_gap()
    repository.set_coin_filter_scan_page("11010", filter_scan.DEEP_DONE_PAGE)
    with db.connect() as conn, conn:
        conn.execute(
            "UPDATE coin_filter_scan_progress SET updated_at = ? WHERE category_code = '11010'",
            ("2020-01-01T00:00:00",),
        )
    fake = _FakeApi({page: [_item(page)] for page in range(1, 24)})
    monkeypatch.setattr(filter_scan, "MxiqiGapi", lambda cfg: fake)
    monkeypatch.setattr(
        repository, "list_coin_categories", lambda parent: [{"code": "11010"}]
    )
    monkeypatch.setattr(filter_scan, "DAILY_QUOTA", 30)
    monkeypatch.setattr(filter_scan, "_pause", _noop)

    asyncio.run(filter_scan.scan(_cfg(), full=True))

    # 已扫到底但仍有缺口：冷却到期后断点重置为 1，顶部 20 页 + 深扫 21~24（24 页为空）
    assert fake.requested == [*range(1, 25)]


def test_full_scan_keeps_deep_after_stale_pages(temp_db, monkeypatch):
    _seed_gap()
    repository.set_coin_filter_scan_page("11010", 21)
    pages = {page: [_item(page)] for page in range(1, 21)}
    pages[21] = [
        {"auction_id": 1, "item_id": 999, "update_time": "2020-01-01 00:00:00"}
    ]  # 深处第 21 页整体很旧：不再据此停扫
    fake = _FakeApi(pages)
    monkeypatch.setattr(filter_scan, "MxiqiGapi", lambda cfg: fake)
    monkeypatch.setattr(
        repository, "list_coin_categories", lambda parent: [{"code": "11010"}]
    )
    monkeypatch.setattr(filter_scan, "DAILY_QUOTA", 30)
    monkeypatch.setattr(filter_scan, "_pause", _noop)

    result = asyncio.run(filter_scan.scan(_cfg(), full=True))

    assert fake.requested == [*range(1, 23)]  # 旧页不停，扫到空页才收
    assert result.stopped is None
    assert repository.coin_filter_scan_pages() == {"11010": filter_scan.DEEP_DONE_PAGE}


def test_full_scan_skips_deep_when_no_uncovered_sold(temp_db, monkeypatch):
    fake = _FakeApi({page: [_item(page)] for page in range(1, 24)})
    monkeypatch.setattr(filter_scan, "MxiqiGapi", lambda cfg: fake)
    monkeypatch.setattr(
        repository, "list_coin_categories", lambda parent: [{"code": "11010"}]
    )
    monkeypatch.setattr(filter_scan, "DAILY_QUOTA", 30)
    monkeypatch.setattr(filter_scan, "_pause", _noop)

    asyncio.run(filter_scan.scan(_cfg(), full=True))

    assert fake.requested == [*range(1, 21)]  # 没有未回填成交：只扫顶部 20 页


def test_incremental_scan_stops_after_pages_without_db_hits(temp_db, monkeypatch):
    unknown = {"auction_id": 1, "item_id": 999, "update_time": "2020-01-01 00:00:00"}
    fake = _FakeApi({page: [unknown] for page in range(1, 13)})
    monkeypatch.setattr(filter_scan, "MxiqiGapi", lambda cfg: fake)
    monkeypatch.setattr(
        repository, "list_coin_categories", lambda parent: [{"code": "11010"}]
    )
    monkeypatch.setattr(filter_scan, "_yesterday_gap", lambda cfg: {"11010": 1})
    monkeypatch.setattr(filter_scan, "DAILY_QUOTA", 30)
    monkeypatch.setattr(filter_scan, "_pause", _noop)

    result = asyncio.run(filter_scan.scan(_cfg(), full=False))

    assert fake.requested == [*range(1, 11)]  # 连续 10 页无库内命中才停，与 update_time 无关
    assert result.stopped == "incremental_no_gain"
    cursor = f"{filter_scan.INCREMENTAL_CURSOR_PREFIX}11010"
    assert repository.coin_filter_scan_pages()[cursor] == 11  # 断点保留在下一页


def test_incremental_scan_orders_priority_categories(temp_db, monkeypatch):
    codes = ["11010", "11040", "11020", "11050", "11070"]
    fake = _FakeApi({})
    seen: list[str] = []

    async def _search(search_params: dict, page: int) -> list[dict]:
        seen.append(str(search_params["cate_id_2"]))
        return []

    fake.search_filter_items = _search  # type: ignore[method-assign]
    monkeypatch.setattr(filter_scan, "MxiqiGapi", lambda cfg: fake)
    monkeypatch.setattr(
        repository,
        "list_coin_categories",
        lambda parent: [{"code": code} for code in codes],
    )
    monkeypatch.setattr(filter_scan, "_yesterday_gap", lambda cfg: {code: 1 for code in codes})
    monkeypatch.setattr(filter_scan, "_pause", _noop)

    asyncio.run(filter_scan.scan(_cfg(), full=False))

    assert seen == ["11070", "11050", "11020", "11010", "11040"]


def test_incremental_scan_resumes_saved_cursor_same_day(temp_db, monkeypatch):
    _seed_gap()
    _set_closed_yesterday("11010")
    cursor = f"{filter_scan.INCREMENTAL_CURSOR_PREFIX}11010"
    repository.set_coin_filter_scan_page(cursor, 5)
    fake = _FakeApi({})
    monkeypatch.setattr(filter_scan, "MxiqiGapi", lambda cfg: fake)
    monkeypatch.setattr(
        repository, "list_coin_categories", lambda parent: [{"code": "11010"}]
    )
    monkeypatch.setattr(filter_scan, "_pause", _noop)

    asyncio.run(filter_scan.scan(_cfg(), full=False))

    assert fake.requested == [5]  # 同日重跑从断点页续扫
    assert repository.coin_filter_scan_pages()[cursor] == 0  # 空页 → 本类收工


def test_incremental_scan_ends_category_on_page_error(temp_db, monkeypatch):
    class _FlakyApi(_FakeApi):
        async def search_filter_items(self, search_params: dict, page: int) -> list[dict]:
            self.requested.append(page)
            if page >= 3:  # 模拟站点封顶后第 477 页起恒报「拍品查询失败」
                raise filter_scan.GapiError("拍品查询失败")
            return [_item(page)]

    fake = _FlakyApi({})
    monkeypatch.setattr(filter_scan, "MxiqiGapi", lambda cfg: fake)
    monkeypatch.setattr(
        repository, "list_coin_categories", lambda parent: [{"code": "11010"}]
    )
    monkeypatch.setattr(filter_scan, "_yesterday_gap", lambda cfg: {"11010": 1})
    monkeypatch.setattr(filter_scan, "DAILY_QUOTA", 30)
    monkeypatch.setattr(filter_scan, "REQUEST_RETRIES", 0)
    monkeypatch.setattr(filter_scan, "_pause", _noop)

    result = asyncio.run(filter_scan.scan(_cfg(), full=False))

    assert fake.requested == [1, 2, 3]  # 失败页即收工，不再空打后续页
    assert result.stopped == "page_error"
    cursor = f"{filter_scan.INCREMENTAL_CURSOR_PREFIX}11010"
    assert repository.coin_filter_scan_pages()[cursor] == 3  # 断点停在失败页，次日从第 1 页重扫


def test_allocate_pages_follows_yesterday_weights():
    pages = filter_scan.allocate_pages(
        {"11070": 1039, "11020": 236, "11040": 57, "11050": 38, "11030": 3, "11010": 1, "11060": 0},
        420,
        minimum=1,
    )
    assert sum(pages.values()) == 420  # 配额不超发
    assert pages["11070"] > pages["11020"] > pages["11040"] > pages["11050"]
    assert pages["11060"] == 1  # 昨日无成交也保底 1 页


def test_allocate_pages_splits_evenly_without_weights():
    assert filter_scan.allocate_pages({"a": 0, "b": 0}, 9, minimum=1) == {"a": 5, "b": 4}
    assert filter_scan.allocate_pages({"a": 1}, 0) == {"a": 0}

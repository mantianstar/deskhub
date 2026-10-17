"""麦稀奇采集测试：gapi 解析与管道落库（M8/M9，plan §18.11）。

- **解析**：`app/coins/gapi.py` 的纯函数，喂**真实响应字段**（2026-10-12 实测的值）——
  专场列表 / 专场详情+拍品 / 站内成交，三份 payload 都对应真实接口。
- **管道测试**：用**返回对象的替身**（一个 gapi 替身同时扮采集与查价），验的是编排与落库，与网络无关。

运行方式：`.venv/bin/python -m pytest tests/test_coins_fetch.py -q`
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import config as config_module
from app import db, repository
from app.coins import gapi
from app.coins.gapi import LotInfo, SessionCard, SessionDetail, ShopInfo
from app.coins.pipeline import run as coins_run
from app.models import CoinFetchStatus

SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


# ---------------------------------------------------------------- gapi 解析（真实响应字段）
# 下面三份 payload 的字段名/取值都是 2026-10-12 从 gapi 真实响应里抄的（328115 / 6863378 / 成交搜索）。

LIST_ITEM = {
    "auction_id": 329043,
    "auction_mode": 2,
    "name": "沼泽如烟--第1236场",
    "num": 255,
    "status": 1,
    "end_time": "2026-10-12 20:00:00",
    "stat": {"stat_id": 329043, "auction_id": 329043, "pv": 2866, "praise": 0},
    "org_info": {
        "oid": 2256,
        "owner_id": 41006,
        "owner_name": "沼泽如烟",
        "thumb": "https://thirdwx.qlogo.cn/mmopen/vi_32/abc/132",
        "owner_verified_status": 1,
    },
    "thumbs": [{"thumb": "V1/AUC/20261010/IMG/c77052e21de2e", "sort": 1}],
}

DETAIL_PAYLOAD = {
    "cdn": {"oss": "https://oss.mxiqi.com/"},
    "auction": {
        "auction_id": 328115,
        "name": "沼泽如烟--第1230场",
        "status": 1,
        "num": 128,
        "start_time": "2026-10-06 21:25:05",
        "end_time": "2026-10-12 19:30:00",
        "price_deposit": 2000,
    },
    "org_info": {
        "oid": 2256,
        "owner_id": 41006,
        "name": "沼泽如烟",
        "thumb": "https://thirdwx.qlogo.cn/mmopen/vi_32/abc/132",
        "status_verify": 1,
        "register_years": 4,
        "deposit": 49960,
    },
    "auction_stat": {"stat_id": 328115, "auction_id": 328115, "pv": 7127, "praise": 0},
    "auction_items": {
        "page": 1,
        "pageSize": 128,
        "totalSize": 128,
        "totalPage": 1,
        "items": [
            {
                "item_id": 6863378,
                "order_no": 1,
                "name": "1981 Yuan Sun-B5a Great Wall",
                "price": 25800,
                "price_start": 0,
                "attrs": [{"attr_name": "评级公司", "attr_value": "PCGS"}],
                "thumbs": [{"thumb": "V1/AUC/20261006/IMG/eae82305fe564", "sort": 1}],
            }
        ],
    },
}

SOLD_PAYLOAD = {
    "auction_items": {
        "page": 1,
        "pageSize": 21,
        "totalSize": 383,
        "totalPage": 19,
        "items": [
            {
                "item_id": 6888122,
                "name": "英国1960年",
                "price": 1200,
                "status": 2,  # 成交
                "end_time": "2026-09-01 10:00:00",
            },
            {
                "item_id": 999,
                "name": "还在拍的",
                "price": 0,
                "status": 1,  # 在拍，不是成交
                "end_time": None,
            },
        ],
    }
}


def test_format_and_parse_site_time():
    assert gapi.format_end_at("2026-10-12 19:30:00") == "10月12日 星期一 19:30"
    assert gapi.format_end_at("2026-10-06 21:25:05") == "10月6日 星期二 21:25"
    assert gapi.format_end_at(None) is None
    assert gapi.format_end_at("不是时间") is None


def test_to_utc_iso_treats_site_time_as_business_timezone():
    """站内时间是**朴素北京时间**，落库统一换成 UTC。"""
    assert gapi.to_utc_iso("2026-10-12 19:30:00", SHANGHAI) == "2026-10-12T11:30:00Z"
    assert gapi.to_utc_iso(None, SHANGHAI) is None


def test_card_from_auction_maps_list_card():
    card = gapi.card_from_auction(LIST_ITEM)

    assert card is not None
    assert card.mxiqi_id == "329043"
    assert card.title == "沼泽如烟--第1236场"
    assert card.status == "running"  # status=1 → 进行中
    assert card.auction_at_raw == "10月12日 星期一 20:00"  # 截拍时间
    assert card.lot_total == 255
    assert card.view_count == 2866
    assert card.cover_urls == ["https://oss.mxiqi.com/V1/AUC/20261010/IMG/c77052e21de2e"]

    assert card.shop is not None
    assert card.shop.mxiqi_uid == "41006"  # 站内 /user.main.entry/{uid}
    assert card.shop.name == "沼泽如烟"
    assert [badge["label"] for badge in card.shop.badges] == ["认证"]


def test_detail_from_payload_maps_session_shop_and_lots():
    detail = gapi.detail_from_payload(DETAIL_PAYLOAD)

    card = detail.card
    assert card.mxiqi_id == "328115"
    assert card.title == "沼泽如烟--第1230场"
    assert card.status == "running"
    assert card.auction_at_raw == "10月12日 星期一 19:30"  # 截拍时间（end_time）
    assert card.lot_total == 128
    assert card.view_count == 7127
    # 封面图取前几条拍品的图（专场层没有封面字段）
    assert card.cover_urls == ["https://oss.mxiqi.com/V1/AUC/20261006/IMG/eae82305fe564"]

    assert detail.deposit == 20.0  # price_deposit=2000（分）→ ¥20
    assert card.shop is not None
    assert card.shop.mxiqi_uid == "41006"
    assert [badge["label"] for badge in card.shop.badges] == ["认证", "保证金", "4年店铺"]

    assert len(detail.lots) == 1
    lot = detail.lots[0]
    assert lot.mxiqi_id == "6863378"
    assert lot.lot_no == "Lot.1"  # order_no=1
    assert lot.start_price == 0.0  # price_start=0
    assert lot.current_price == 258.0  # price=25800（分）
    assert lot.image_urls == ["https://oss.mxiqi.com/V1/AUC/20261006/IMG/eae82305fe564"]
    assert lot.tags == {"评级公司": "PCGS"}  # attrs → 标签


def test_detail_from_payload_falls_back_to_list_card():
    """详情缺字段时用列表页那张卡兜底。"""
    card = gapi.card_from_auction(LIST_ITEM)
    payload = {"auction": {"auction_id": 329043}, "auction_items": {"items": []}}

    detail = gapi.detail_from_payload(payload, card)

    assert detail.card.title == "沼泽如烟--第1236场"
    assert detail.card.lot_total == 255
    assert detail.card.view_count == 2866
    assert detail.card.cover_urls == card.cover_urls
    assert detail.card.shop is not None and detail.card.shop.mxiqi_uid == "41006"


def test_session_status_codes_map_to_three_states():
    for code, expected in ((0, "preview"), (1, "running"), (2, "closed")):
        assert gapi.SESSION_STATUS_BY_CODE[code] == expected
    assert gapi.card_from_auction({**LIST_ITEM, "status": 2}).status == "closed"


def test_sold_from_payload_keeps_only_sold_items():
    """只有 `status=2`（成交）才当样本；价格是分，`end_time` 当成交时间。"""
    samples = gapi.sold_from_payload(SOLD_PAYLOAD, SHANGHAI)

    assert [sample.title for sample in samples] == ["英国1960年"]
    assert samples[0].price == 12.0  # 1200 分
    assert samples[0].sold_at == "2026-09-01T02:00:00Z"


class _StubApi(gapi.MxiqiGapi):
    """只替换 `_call`：验的是「请求了哪个动作 / 参数对不对 / 去重 / 状态过滤」，与网络无关。"""

    def __init__(self, by_mode: dict[int, list[dict]], detail: dict | None = None) -> None:
        super().__init__(config_module.load())
        self._by_mode = by_mode
        self._detail = detail or DETAIL_PAYLOAD
        self.calls: list[tuple[str, dict]] = []

    async def _call(self, action: str, payload: dict) -> dict:
        self.calls.append((action, payload))
        if action == gapi.ACTION_LIST:
            return {"auctions": {"items": self._by_mode[payload["auction_mode"]]}}
        if action == gapi.ACTION_FILTER:
            return SOLD_PAYLOAD
        return self._detail


@pytest.mark.asyncio
async def test_list_sessions_merges_both_modes_and_dedupes():
    """实时（mode=1）+ 限时（mode=2）两路都取，按专场 id 去重，并只留预展/进行中。"""
    closed = {**LIST_ITEM, "auction_id": 999, "status": 2, "name": "已公示的场"}
    api = _StubApi({1: [LIST_ITEM], 2: [LIST_ITEM, closed]})

    cards = await api.list_sessions()

    assert [call[1]["auction_mode"] for call in api.calls] == [1, 2]
    assert [card.mxiqi_id for card in cards] == ["329043"]  # 去重 + 滤掉公示


@pytest.mark.asyncio
async def test_session_detail_sets_end_at_in_utc():
    api = _StubApi({1: [], 2: []})

    detail = await api.session_detail("328115")

    assert detail.end_at == "2026-10-12T11:30:00Z"  # 截拍时间的 UTC ISO
    assert api.calls[-1] == (gapi.ACTION_GET, {"auction_id": 328115})


@pytest.mark.asyncio
async def test_search_sold_posts_keywords_and_status():
    api = _StubApi({1: [], 2: []})

    samples = await api.search_sold("英国 1960")

    assert api.calls[-1] == (
        gapi.ACTION_FILTER,
        {"search_params": {"keywords": "英国 1960", "status": gapi.SOLD_STATUS}},
    )
    assert [sample.title for sample in samples] == ["英国1960年"]


@pytest.mark.asyncio
async def test_search_sold_skips_blank_keyword():
    api = _StubApi({1: [], 2: []})

    assert await api.search_sold("   ") == []
    assert api.calls == []  # 空关键词不发请求


# ---------------------------------------------------------------- 管道（对象替身）
# 这一段验的是「编排 + 落库」：一个替身同时扮采集与查价，都不碰网络。


def _today_end_at() -> str:
    """今天正午（业务时区）的 UTC ISO：满足「只抓当日截拍专场」。"""
    noon = datetime.now(SHANGHAI).replace(hour=12, minute=0, second=0, microsecond=0)
    return noon.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _card(mxiqi_id: str = "328062") -> SessionCard:
    return SessionCard(
        mxiqi_id=mxiqi_id,
        title="东印度硬币专场",
        status="running",
        auction_at_raw="10月12日 星期一 19:30",
        lot_total=50,
        view_count=79,
        cover_urls=[f"https://oss.mxiqi.com/V1/AUC/c{i}" for i in range(4)],
        shop=ShopInfo(
            mxiqi_uid="49528",
            name="猩哥外币小铺",
            avatar_url="https://oss.mxiqi.com/V1/USR/avatar",
            badges=[{"kind": "认证", "label": "认证"}, {"kind": "years", "label": "2年店铺"}],
        ),
        end_at=_today_end_at(),
    )


def _detail(mxiqi_id: str) -> SessionDetail:
    return SessionDetail(
        card=_card(mxiqi_id),
        deposit=10.0,
        end_at=_today_end_at(),
        lots=[
            LotInfo(
                mxiqi_id="1870684",
                lot_no="Lot.1",
                title="东印度公司1808年硬币",
                start_price=15.0,
                current_price=0.0,
                image_urls=[f"https://oss.mxiqi.com/V1/AUC/l{i}" for i in range(3)],
                tags={"发行年份": "1808"},
            )
        ],
    )


class _FakeGapi:
    """替身：接口与 `gapi.MxiqiGapi` 一致，但不发请求（采集 + 查价都由它扮）。"""

    def __init__(self, *, fail_list: bool = False, sold: list | None = None) -> None:
        self._fail_list = fail_list
        self._sold = sold or []
        self.detail_calls: list[str] = []
        self.sold_calls: list[str] = []

    async def list_sessions(self) -> list[SessionCard]:
        if self._fail_list:
            raise gapi.GapiError("网关 502")
        return [_card(), _card("323864")]

    async def session_detail(
        self, mxiqi_id: str, *, fallback: SessionCard | None = None
    ) -> SessionDetail:
        self.detail_calls.append(mxiqi_id)
        return _detail(mxiqi_id)

    async def search_sold(self, keyword: str) -> list:
        self.sold_calls.append(keyword)
        return self._sold


def _seed_tags() -> None:
    """让「东印度公司1808年硬币」能命中（权重和 2.0 >= 阈值 1.5）。"""
    repository.upsert_coin_tag("era", "1808", 1.0, 4)
    repository.upsert_coin_tag("denomination", "硬币", 1.0, 4)


def _cfg(**price_overrides):
    """真配置 + 查价限速归零（真实配置里是 15 秒，测试不该真等）。"""
    cfg = config_module.load()
    price = replace(cfg.coins.price, query_interval_seconds=0, **price_overrides)
    return replace(cfg, coins=replace(cfg.coins, price=price))


def _sold():
    from app.coins.pricing import SoldSample

    return [SoldSample("东印度1808", 18), SoldSample("东印度1808", 22), SoldSample("东印度1808", 30)]


@pytest.mark.asyncio
async def test_pipeline_collects_and_matches(temp_db):
    _seed_tags()
    cfg = config_module.load()
    api = _FakeGapi()

    result = await coins_run(cfg, gapi=api, collect_only=True)

    assert result.status == CoinFetchStatus.OK
    assert result.sessions_stored == 2  # 两个专场用的是同一个替身详情
    # 拍品去重键是「专场 id:站内拍品 id」，所以两条不会互相覆盖
    assert result.lots_stored == 2
    assert result.lots_recommended == 2

    cards = repository.list_coin_session_cards()
    assert len(cards) == 2
    assert all(card.recommended_count == 1 for card in cards)
    assert all(card.view_count == 79 for card in cards)  # 来自列表页那张卡
    # 截拍时间落了库（UTC ISO + 原始文案）
    detail = repository.get_coin_session_detail(cards[0].session_id)
    assert detail is not None
    assert detail.auction_at == _today_end_at()
    assert detail.auction_at_raw == "10月12日 星期一 19:30"


@pytest.mark.asyncio
async def test_pipeline_records_and_continues_when_list_fails(temp_db):
    """列表拉取失败只记一条日志，不影响后续（库里已有的照常匹配）。"""
    _seed_tags()
    cfg = config_module.load()

    result = await coins_run(cfg, gapi=_FakeGapi(fail_list=True), collect_only=True)

    assert result.sessions_stored == 0
    summary = repository.coin_fetch_summary()
    assert summary["last_status"] != "ok"


@pytest.mark.asyncio
async def test_pipeline_prices_and_records_evidence(temp_db):
    _seed_tags()
    cfg = _cfg()
    api = _FakeGapi(sold=_sold())

    result = await coins_run(cfg, gapi=api)

    assert result.lots_priced == 2
    assert api.sold_calls, "应该至少查了一次成交"
    lot = next(lot for lot in repository.list_coin_lots_for_matching() if lot.is_recommended)
    ref = repository.get_coin_price_ref(lot.id)
    assert ref is not None
    assert ref.sample_count == 3
    assert ref.median == 22.0  # 中位数
    assert "东印度1808" in ref.raw


@pytest.mark.asyncio
async def test_pipeline_price_respects_per_run_cap(temp_db):
    """单轮查价有上限；超出的留到下一轮补。"""
    _seed_tags()
    cfg = _cfg(max_queries_per_run=1)
    api = _FakeGapi(sold=_sold())

    first = await coins_run(cfg, gapi=api)

    assert first.lots_priced == 1  # 上限 1 条
    assert first.lots_pricing_deferred == 1  # 另一条推迟
    assert len(api.sold_calls) == 1

    second = await coins_run(cfg, gapi=api, price_only=True)

    assert second.lots_priced == 1  # 上轮推迟的这条这轮补上
    assert second.lots_pricing_skipped == 1  # 上轮查过的当日不再查


@pytest.mark.asyncio
async def test_pipeline_skips_pricing_when_keyword_too_generic(temp_db):
    """命中里没有面值/品类 → 关键词太泛，**主动不报价**（否则会报出离谱的价）。

    实测背景：当前价 ¥330 的「巴拿马1967年精制套币」被关键词「巴拿马 1967」报成 ¥2。
    """
    # 只给国家 + 年代：总分 2.0 够推荐，但拼出来的关键词太泛
    repository.upsert_coin_tag("country", "东印度", 1.0, 4)
    repository.upsert_coin_tag("era", "1808", 1.0, 4)
    cfg = _cfg()
    api = _FakeGapi(sold=_sold())

    result = await coins_run(cfg, gapi=api)

    assert result.lots_recommended == 2
    assert result.lots_priced == 0
    assert result.lots_pricing_too_generic == 2
    assert api.sold_calls == []  # 压根没发请求


@pytest.mark.asyncio
async def test_pipeline_price_skips_already_priced_today(temp_db):
    """当日已查过价的拍品不再重复请求（也是限速的一部分）。"""
    _seed_tags()
    cfg = _cfg()
    api = _FakeGapi(sold=_sold())

    first = await coins_run(cfg, gapi=api)
    calls_after_first = len(api.sold_calls)

    second = await coins_run(cfg, gapi=api, price_only=True)

    assert len(api.sold_calls) == calls_after_first  # 当日已查过 → 不重复请求
    assert second.lots_pricing_skipped == first.lots_priced

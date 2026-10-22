"""麦稀奇采集测试：gapi 解析与管道落库（M8/M9，plan §18.11）。

- **解析**：`app/coins/gapi.py` 的纯函数，喂**真实响应字段**（2026-10-12 实测的值）——
  专场列表 / 专场详情+拍品 / 站内成交，三份 payload 都对应真实接口。
- **管道测试**：用**返回对象的替身**（一个 gapi 替身同时扮采集与查价），验的是编排与落库，与网络无关。

运行方式：`.venv/bin/python -m pytest tests/test_coins_fetch.py -q`
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import config as config_module
from app import db, repository
from app.coins import gapi
from app.coins.gapi import LotInfo, SessionCard, SessionDetail, ShopInfo
from app.coins.pipeline import run as coins_run
from app.migrations import v18
from app.models import CoinFetchStatus, CoinLot, CoinSale, CoinSession

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


def test_card_from_auction_maps_live_mode_to_start_time():
    """实时专场（auction_mode=1）的时间取开拍 start_time，类型记为 live。"""
    live = {**LIST_ITEM, "auction_mode": 1, "start_time": "2026-10-12 19:30:00"}

    card = gapi.card_from_auction(live, tz=SHANGHAI)

    assert card is not None
    assert card.kind == "live"
    assert card.auction_at_raw == "10月12日 星期一 19:30"  # 开拍时间（start_time）
    assert card.start_at == "2026-10-12T11:30:00Z"


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


# 运费 / 包邮 / 寄存的真实文案（2026-10-22 实测 330227）
EXPRESS_DESC = (
    '<div style="margin-bottom:4px;">运费 ¥8，另外吉林省¥10；甘肃省¥12；青海省¥12；'
    "西藏自治区¥18；新疆维吾尔自治区¥18；</div>"
)
POLICY_DESC = (
    "<p>1、所有藏品包老保真，实物拍摄，按图发货。</p>"
    "<p>2、满百  包邮，不作预留。</p>"
    "<p>3、不到包邮条件订单，支付运费即发货，亦可留言寄存凑单，寄存时间最长不超15天。</p>"
)


def test_detail_from_payload_maps_commission_and_express():
    auction = {
        **DETAIL_PAYLOAD["auction"],
        "commission_rate": 0.03,
        "commission_min": 0,
        "express_day": 1,
        "express_desc": EXPRESS_DESC,
        "desc_orginal": POLICY_DESC,
    }

    detail = gapi.detail_from_payload({**DETAIL_PAYLOAD, "auction": auction})

    assert detail.commission_rate == 0.03
    assert detail.commission_min == 0.0
    assert detail.express_day == 1
    assert detail.express_desc == EXPRESS_DESC  # 富文本原样存
    assert detail.express_fee == 8.0  # 只取基础运费，忽略地区价
    assert detail.express_free_amount == 100.0  # 「满百包邮」
    assert detail.is_storage is True


def test_express_parsers_handle_missing_text():
    assert gapi.parse_express_fee(None) is None
    assert gapi.parse_express_free_amount("") is None
    assert gapi.parse_is_storage("") is False
    assert gapi.parse_express_free_amount("满 200 元包邮") == 200.0


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
async def test_session_detail_keeps_commission_and_express_fields():
    """详情入口补 `end_at` 时不能把费率 / 运费字段丢掉（v17 回归）。"""
    payload = {
        **DETAIL_PAYLOAD,
        "auction": {
            **DETAIL_PAYLOAD["auction"],
            "commission_rate": 0.03,
            "commission_min": 0,
            "express_day": 1,
            "express_desc": EXPRESS_DESC,
            "desc_orginal": POLICY_DESC,
        },
    }
    api = _StubApi({1: [], 2: []}, detail=payload)

    detail = await api.session_detail("328115")

    assert detail.commission_rate == 0.03
    assert detail.express_fee == 8.0
    assert detail.express_free_amount == 100.0
    assert detail.is_storage is True


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


# ---------------------------------------------------------------- 风控（真 _call + MockTransport）


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://gapi.test", transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_call_retries_once_after_429(monkeypatch):
    """429 先退避再重试；第二次成功就正常返回（退避归零，测试不真等）。"""
    monkeypatch.setattr(gapi, "RETRY_BACKOFF_SECONDS", 0)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"data": {"ok": 1}})

    async with gapi.MxiqiGapi(config_module.load(), client=_mock_client(handler)) as api:
        data = await api._call("miniprogram.test", {})

    assert data == {"ok": 1}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_call_gives_up_after_retries(monkeypatch):
    """连续 429 只重试 RETRY_ATTEMPTS 次，最终抛 GapiError（交给管道记失败）。"""
    monkeypatch.setattr(gapi, "RETRY_BACKOFF_SECONDS", 0)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429)

    async with gapi.MxiqiGapi(config_module.load(), client=_mock_client(handler)) as api:
        with pytest.raises(gapi.GapiError):
            await api._call("miniprogram.test", {})

    assert len(calls) == gapi.RETRY_ATTEMPTS + 1


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
        commission_rate=0.03,
        commission_min=0.0,
        express_desc="运费 ¥8",
        express_day=1,
        express_fee=8.0,
        express_free_amount=100.0,
        is_storage=True,
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
    """真配置 + 采集/查价限速归零（真实配置里是 3 / 15 秒，测试不该真等）。"""
    cfg = config_module.load()
    price = replace(cfg.coins.price, query_interval_seconds=0, **price_overrides)
    return replace(
        cfg, coins=replace(cfg.coins, request_interval_seconds=0, price=price)
    )


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
    # 采集入口不再顺带回采终态（回采在控制页是独立功能）
    assert result.sessions_finalized == 0

    cards = repository.list_coin_session_cards()
    assert len(cards) == 2
    assert all(card.recommended_count == 1 for card in cards)
    assert all(card.view_count == 79 for card in cards)  # 来自列表页那张卡
    # 截拍时间落了库（UTC ISO + 原始文案）
    detail = repository.get_coin_session_detail(cards[0].session_id)
    assert detail is not None
    assert detail.auction_at == _today_end_at()
    assert detail.auction_at_raw == "10月12日 星期一 19:30"
    # 费率 / 运费 / 包邮门槛 / 寄存一并落库（plan §18.18.1 P1-8）
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT commission_rate, commission_min, express_desc, express_day,
                   express_fee, express_free_amount, is_storage
            FROM coin_sessions LIMIT 1
            """
        ).fetchone()
    assert tuple(row) == (0.03, 0.0, "运费 ¥8", 1, 8.0, 100.0, 1)


@pytest.mark.asyncio
async def test_pipeline_collects_live_session_by_start_time(temp_db):
    """实时专场按「今日开拍」采集：end_at 为空也不影响，入库类型为 live。"""

    class _LiveGapi(_FakeGapi):
        async def list_sessions(self) -> list[SessionCard]:
            return [replace(_card(), kind="live", start_at=_today_end_at(), end_at=None)]

        async def session_detail(self, mxiqi_id: str, *, fallback=None) -> SessionDetail:
            return replace(
                _detail(mxiqi_id),
                card=replace(_card(mxiqi_id), kind="live", start_at=_today_end_at(), end_at=None),
            )

    result = await coins_run(config_module.load(), gapi=_LiveGapi(), collect_only=True)

    assert result.sessions_stored == 1
    cards = repository.list_coin_session_cards(only_recommended=False)
    assert [card.kind for card in cards] == ["live"]


@pytest.mark.asyncio
async def test_pipeline_waits_between_session_details(temp_db, monkeypatch):
    """采集逐场请求按 `request_interval_seconds` 抖动限速：两个专场只等一次。"""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    cfg = config_module.load()
    cfg = replace(cfg, coins=replace(cfg.coins, request_interval_seconds=3))

    await coins_run(cfg, gapi=_FakeGapi(), collect_only=True)

    assert len(delays) == 1
    assert 1.8 <= delays[0] <= 4.2  # 3 秒 × (0.6 ~ 1.4)


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


# ---------------------------------------------------------------- 终态回采（plan/coins.md §18.18）


def test_lot_from_item_parses_terminal_fields():
    """status 2/3 落终态；在拍（1）与未回采必须为空。"""
    sold = gapi.lot_from_item(
        {
            "item_id": 1870684,
            "name": "东印度公司1808年硬币",
            "status": 2,
            "price": 8800,
            "price_start": 1500,
            "won_user_id": 9001,
            "end_time": "2026-10-21 14:00:01",
        },
        tz=SHANGHAI,
    )
    assert sold.outcome == "sold"
    assert sold.final_price == 88.0
    assert sold.winner_uid == "9001"
    assert sold.closed_at == "2026-10-21T06:00:01Z"

    unsold = gapi.lot_from_item(
        {"item_id": 2, "name": "未成交拍品", "status": 3, "end_time": "2026-10-21 14:00:01"},
        tz=SHANGHAI,
    )
    assert unsold.outcome == "unsold"
    assert unsold.final_price is None

    running = gapi.lot_from_item({"item_id": 3, "name": "在拍拍品", "status": 1}, tz=SHANGHAI)
    assert running.outcome is None
    assert running.closed_at is None


def _hours_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _seed_ended_session() -> int:
    """库里一个 10 小时前结束、状态仍是「进行中」、拍品还没有终态的专场。"""
    session_id = repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id="330227",
            shop_id=None,
            title="已结束专场",
            status="running",
            kind="timed",
            auction_at=_hours_ago(10),
            auction_at_raw="10月20日 星期二 20:00",
            deposit=0.0,
            lot_total=2,
            view_count=10,
            cover_urls=[],
            fetched_at=_hours_ago(10),
        )
    )
    for item_id, title in (("1870684", "东印度公司1808年硬币"), ("1870685", "另一枚硬币")):
        repository.upsert_coin_lot(
            CoinLot(
                id=None,
                session_id=session_id,
                mxiqi_id=f"330227:{item_id}",
                lot_no=None,
                title=title,
                start_price=15.0,
                current_price=0.0,
                image_urls=[],
                tags={},
                match_score=0.0,
                is_recommended=False,
                fetched_at=_hours_ago(10),
            )
        )
    return session_id


class _FinalizeGapi(_FakeGapi):
    """列表返回空（本轮不采新场），只扮「已结束专场的详情」。"""

    async def list_sessions(self):
        return []

    async def session_detail(self, mxiqi_id: str, *, fallback=None) -> SessionDetail:
        self.detail_calls.append(mxiqi_id)
        return SessionDetail(
            card=replace(_card(mxiqi_id), status="closed"),
            deposit=0.0,
            end_at=_hours_ago(10),
            lots=[
                LotInfo(
                    mxiqi_id="1870684",
                    lot_no="Lot.1",
                    title="东印度公司1808年硬币",
                    start_price=15.0,
                    current_price=88.0,
                    image_urls=[],
                    tags={},
                    outcome="sold",
                    final_price=88.0,
                    winner_uid="9001",
                    closed_at=_hours_ago(10),
                ),
                LotInfo(
                    mxiqi_id="1870685",
                    lot_no="Lot.2",
                    title="另一枚硬币",
                    start_price=15.0,
                    current_price=None,
                    image_urls=[],
                    tags={},
                    outcome="unsold",
                    closed_at=_hours_ago(10),
                ),
            ],
        )


@pytest.mark.asyncio
async def test_pipeline_finalizes_ended_session(temp_db):
    """已结束专场：每场 1 次详情即落成交价/流拍/中拍人；补完不再重复抓。"""
    session_id = _seed_ended_session()
    cfg = _cfg()
    api = _FinalizeGapi()

    result = await coins_run(cfg, gapi=api, finalize_only=True)

    assert result.sessions_finalized == 1
    assert (result.lots_sold, result.lots_unsold) == (1, 1)
    # 回采时同步拍场状态：进行中 → 已截拍
    assert repository.get_coin_session_card(session_id).status == "closed"
    lots = {lot.mxiqi_id: lot for lot in repository.list_coin_lots_for_matching()}
    assert lots["330227:1870684"].outcome == "sold"
    assert lots["330227:1870684"].final_price == 88.0
    assert lots["330227:1870684"].winner_uid == "9001"
    assert lots["330227:1870685"].outcome == "unsold"
    assert lots["330227:1870685"].final_price is None

    # 在拍场次重抓（终态全空）不能把已回采的终态擦掉
    repository.upsert_coin_lot(
        replace(lots["330227:1870684"], outcome=None, final_price=None,
                winner_uid=None, closed_at=None)
    )
    again = {lot.mxiqi_id: lot for lot in repository.list_coin_lots_for_matching()}
    assert again["330227:1870684"].outcome == "sold"
    assert again["330227:1870684"].final_price == 88.0

    # 全部落终态后再跑一轮：不再请求详情
    second = await coins_run(cfg, gapi=api, finalize_only=True)
    assert second.sessions_finalized == 0
    assert api.detail_calls == ["330227"]

    # 成交库：终态直接落 session_final，按 item_id 去重（不靠标题）
    sales = repository.list_coin_sales()
    assert len(sales) == 1
    assert (sales[0].item_id, sales[0].price, sales[0].source) == ("1870684", 88.0, "session_final")
    assert result.sales_session_final == 1
    assert result.sales_search == 0


def test_coin_sales_prefers_session_final(temp_db):
    """同一 item_id：高置信行不被中置信覆盖，重复写入只有 1 行。"""
    base = {
        "id": None,
        "title": "东印度公司1808年硬币",
        "sold_at": "2026-10-20T12:00:00Z",
        "session_id": None,
        "shop_id": None,
        "fetched_at": repository.utcnow(),
    }
    repository.upsert_coin_sales(
        [CoinSale(item_id="42", price=10.0, keyword="东印度", source="sold_search", **base)]
    )
    repository.upsert_coin_sales(
        [CoinSale(item_id="42", price=88.0, keyword=None, source="session_final", **base)]
    )
    assert repository.count_coin_sales_by_source() == {"session_final": 1}

    # 中置信再写入：行数不增，价格与来源保持高置信
    repository.upsert_coin_sales(
        [CoinSale(item_id="42", price=9.0, keyword="东印度", source="sold_search", **base)]
    )
    row = repository.list_coin_sales()[0]
    assert (row.source, row.price) == ("session_final", 88.0)


# ---------------------------------------------------------------- 热度与同图识别（plan §18.18.1 P1-6 / P1-7）


PREVIEW_CARD = {
    "auction_id": 331783,
    "auction_mode": 1,
    "name": "实时专场",
    "status": 1,
    "start_time": "2026-10-22 20:00:00",
    "auction_items": {
        "items": [
            {
                "item_id": 7001,
                "name": "预览拍品",
                "price": 1200,
                "price_start": 0,
                "bid_num": 7,
                "bid_user_num": 4,
                "thumbs": [{"thumb": "V1/AUC/20261022/IMG/abc?x=1"}],
            },
            {"item_id": 7002, "name": "预览无热度", "price": 0},
        ]
    },
}


def test_card_preview_keeps_bid_counts_and_oss_keys():
    card = gapi.card_from_auction(PREVIEW_CARD)

    assert [lot.mxiqi_id for lot in card.preview_lots] == ["7001", "7002"]
    first, second = card.preview_lots
    assert (first.bid_num, first.bid_user_num, first.bid_source) == (7, 4, "preview")
    assert first.image_keys == ["V1/AUC/20261022/IMG/abc"]  # 去 query
    assert second.bid_num is None and second.bid_source is None


def test_auction_get_lot_leaves_bid_columns_null():
    """auction.get 的 item 没有 bid_num / bid_user_num，解析结果必须是 NULL。"""
    lot = gapi.lot_from_item(
        {
            "item_id": 1,
            "name": "详情拍品",
            "price": 100,
            "thumbs": [{"thumb": "https://oss.mxiqi.com/V1/AUC/20261022/IMG/x?w=2"}],
        }
    )

    assert (lot.bid_num, lot.bid_user_num, lot.bid_source) == (None, None, None)
    assert lot.image_keys == ["V1/AUC/20261022/IMG/x"]  # 去 base、去 query


def test_sold_from_payload_keeps_bid_counts():
    samples = gapi.sold_from_payload(
        {
            "auction_items": {
                "items": [
                    {
                        "item_id": 7001,
                        "name": "英国1960年",
                        "price": 1200,
                        "status": 2,
                        "end_time": "2026-09-01 10:00:00",
                        "bid_num": 5,
                        "bid_user_num": 3,
                    }
                ]
            }
        },
        SHANGHAI,
    )

    assert (samples[0].bid_num, samples[0].bid_user_num) == (5, 3)


def _seed_running_session(mxiqi_id: str) -> int:
    return repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id=mxiqi_id,
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


def _bid_lot(
    session_id: int,
    mxiqi_id: str,
    *,
    bid_num: int | None = None,
    bid_user_num: int | None = None,
    source: str | None = None,
    keys: tuple[str, ...] = (),
) -> CoinLot:
    return CoinLot(
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
        bid_user_num=bid_user_num,
        bid_source=source,
        image_keys=list(keys),
    )


def test_coin_lot_bids_survive_detail_upsert_and_coverage_buckets(temp_db):
    session_id = _seed_running_session("900001")
    repository.upsert_coin_lot(
        _bid_lot(
            session_id,
            "900001:7001",
            bid_num=7,
            bid_user_num=4,
            source="preview",
            keys=("V1/AUC/a", "V1/AUC/b"),
        )
    )
    # 详情重抓（没有热度键）不能把已有值擦成 NULL
    repository.upsert_coin_lot(_bid_lot(session_id, "900001:7001"))
    lot = repository.list_coin_lots_for_matching()[0]
    assert (lot.bid_num, lot.bid_user_num, lot.bid_source) == (7, 4, "preview")

    # home.filter 按拍品 id 后缀回填；NULL 不覆盖已有值
    repository.update_coin_lot_bids([("7001", None, 6)])
    lot = repository.list_coin_lots_for_matching()[0]
    assert (lot.bid_num, lot.bid_user_num, lot.bid_source) == (7, 6, "home_filter")

    coverage = {row["source"]: row for row in repository.coin_lot_bid_coverage()}
    assert coverage["home_filter"]["lots_with_bid"] == 1
    assert coverage["home_filter"]["avg_bid_num"] == 7.0  # NULL 不参与均值


def test_coin_lot_images_duplicate_groups(temp_db):
    session_id = _seed_running_session("900002")
    repository.upsert_coin_lot(_bid_lot(session_id, "900002:1", keys=("V1/AUC/same", "V1/AUC/x")))
    repository.upsert_coin_lot(_bid_lot(session_id, "900002:2", keys=("V1/AUC/same",)))
    repository.upsert_coin_lot(_bid_lot(session_id, "900002:3", keys=("V1/AUC/other",)))

    groups = repository.coin_lot_image_duplicates()
    assert [(row["oss_key"], row["lot_count"]) for row in groups] == [("V1/AUC/same", 2)]
    assert repository.coin_lot_image_duplicate_lot_count() == 2

    # 重抓时图集按当前结果重建
    repository.upsert_coin_lot(_bid_lot(session_id, "900002:2", keys=("V1/AUC/new",)))
    assert repository.coin_lot_image_duplicates() == []


def test_v18_backfills_oss_keys_from_image_urls(temp_db):
    """老库只有 image_urls：迁移要能把 URL 转成 OSS key（去 base、去 query）。"""
    session_id = _seed_running_session("900003")
    repository.upsert_coin_lot(
        replace(
            _bid_lot(session_id, "900003:1"),
            image_urls=["https://oss.mxiqi.com/V1/AUC/old?w=1"],
        )
    )
    with db.connect() as conn, conn:
        v18.backfill_coin_lot_images(conn)

    with db.connect() as conn:
        keys = [row["oss_key"] for row in conn.execute("SELECT oss_key FROM coin_lot_images")]
    assert keys == ["V1/AUC/old"]

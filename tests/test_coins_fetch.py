"""麦稀奇采集测试：拦截判定、真实 DOM 解析、管道落库（M8/M9，plan §18.11）。

解析用的 fixture 是 **2026-10-12 用 CDP 抓到的真实 HTML 切片**（见文件头注释），不是手工构造：
- `mxiqi-auction.html`：`/auction` 的第一张专场卡
- `mxiqi-session.html`：`/auction.info.entry/327686` 的店铺头 + 前 3 条拍品
- `mxiqi-sold.html`：成交搜索页的前两条成交条目（价格不在 HTML 里，见下）
- `mxiqi-blocked.html`：反爬拦截页的关键片段

管道测试用的是**返回对象的替身**（不解析 HTML）—— 那部分验的是编排与落库，与 DOM 无关。

运行方式：`.venv/bin/python -m pytest tests/test_coins_fetch.py -q`
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app import config as config_module
from app import db, repository
from app.coins import mxiqi
from app.coins.mxiqi import BlockedError, LotInfo, SessionCard, SessionDetail, ShopInfo
from app.coins.pipeline import run as coins_run
from app.models import CoinFetchStatus

FIXTURES = Path(__file__).parent / "fixtures"
AUCTION_HTML = (FIXTURES / "mxiqi-auction.html").read_text(encoding="utf-8")
SESSION_HTML = (FIXTURES / "mxiqi-session.html").read_text(encoding="utf-8")
SOLD_HTML = (FIXTURES / "mxiqi-sold.html").read_text(encoding="utf-8")
BLOCKED_HTML = (FIXTURES / "mxiqi-blocked.html").read_text(encoding="utf-8")


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


# ---------------------------------------------------------------- 拦截判定


def test_looks_blocked_detects_interstitial_and_tiny_pages():
    assert mxiqi.looks_blocked("") is True
    assert mxiqi.looks_blocked("<html>tiny</html>") is True  # 体积过小

    big = "<html><body>" + ("<div>正常内容</div>" * 500) + "</body></html>"
    assert mxiqi.looks_blocked(big) is False

    # captchaV2.js 全站都会引用 —— 拿它当特征会把正常页也判成被拦（2026-10-12 实测踩到）
    with_sdk = big[:-7] + '<script src="https://o.alicdn.com/cdn-fe/custom-slider/captchaV2.js"></script></body></html>'
    assert mxiqi.looks_blocked(with_sdk) is False

    assert mxiqi.looks_blocked(big + "<title>Verification</title>") is True
    padded = BLOCKED_HTML.replace("</body>", ("<!-- pad -->" * 500) + "</body>")
    assert len(padded.encode("utf-8")) > mxiqi.BLOCKED_MAX_BYTES
    assert mxiqi.looks_blocked(padded) is True


def test_looks_usable_requires_expected_content():
    normal = "<html><body>" + ("<div>正常</div>" * 500) + "</body></html>"
    assert mxiqi.looks_usable(normal) is True
    assert mxiqi.looks_usable(normal, ("/auction.info.entry/",)) is False  # 没渲染出专场

    with_entries = normal[:-14] + '<a href="/auction.info.entry/1">x</a></body></html>'
    assert mxiqi.looks_usable(with_entries, ("/auction.info.entry/",)) is True

    padded_blocked = BLOCKED_HTML.replace("</body>", ("<!-- pad -->" * 500) + "</body>")
    assert mxiqi.looks_usable(padded_blocked) is False


def test_interactive_only_for_headful_or_cdp():
    """只有「有人能手动过滑块 / 登录」的模式才给 180 秒等待：headful 或 CDP。"""
    base = config_module.load()
    # 配置里默认不连 CDP 的场景：自己启动 headless —— 没人能操作，不给人工时间
    local = replace(base, coins=replace(base.coins, cdp_url=""))
    assert mxiqi.MxiqiClient(local).interactive is False

    headful = replace(local, coins=replace(local.coins, headless=False))
    assert mxiqi.MxiqiClient(headful).interactive is True

    # 显式传 --cdp，或配置里配了 coins.cdp_url，都算 interactive
    assert mxiqi.MxiqiClient(base, cdp_url="http://127.0.0.1:9222").interactive is True
    assert mxiqi.MxiqiClient(replace(base, coins=replace(base.coins, cdp_url="http://127.0.0.1:9222"))).interactive is True


# ---------------------------------------------------------------- 列表页（真实 DOM）


def test_parse_sessions_from_real_card():
    cards = mxiqi.parse_sessions(AUCTION_HTML)

    assert len(cards) == 1
    card = cards[0]
    assert card.mxiqi_id == "327686"
    assert card.title == "自家阳台上种的洋垃圾🐶"
    assert card.status == "preview"  # 站内写「预出价」
    assert card.auction_at_raw == "11月1日 星期日 04:00"
    assert card.lot_total == 57
    assert card.view_count == 3982
    assert len(card.cover_urls) == 4
    assert all("/AUC/" in url for url in card.cover_urls)  # 只取拍品图，不取头像

    assert card.shop is not None
    assert card.shop.mxiqi_uid == "101298"
    assert card.shop.name == "四角村的狗头人"
    assert card.shop.avatar_url and "/USR/" in card.shop.avatar_url


# ---------------------------------------------------------------- 详情页（真实 DOM）


def test_parse_session_detail_from_real_html():
    detail = mxiqi.parse_session_detail(SESSION_HTML, "327686")

    assert detail.card.title == "自家阳台上种的洋垃圾🐶"
    assert detail.card.status == "preview"  # 详情页写「预展中」
    assert detail.card.auction_at_raw == "11月01日 星期日 04:00"
    assert detail.card.lot_total == 57  # 页面写「共57件」
    assert detail.deposit == 10.0  # 「押 ¥10」

    assert detail.card.shop is not None
    assert detail.card.shop.name == "四角村的狗头人"
    # 徽标按 <span class="wrap"> 结构取，不能被 meta description 里的「保证金」骗到
    assert [badge["label"] for badge in detail.card.shop.badges] == ["认证", "保证金"]

    assert len(detail.lots) == 3  # fixture 里截了前 3 条
    lot = detail.lots[0]
    assert lot.mxiqi_id == "6871280"  # 站内拍品 id（去重键）
    assert lot.lot_no == "Lot.1"
    assert lot.title == "宠物牵引绳（仅试穿）适合颈围小于25cm的小型宠物🐶"
    assert lot.start_price == 0.0
    assert lot.current_price == 0.0
    assert len(lot.image_urls) == 1
    assert all("/AUC/" in url for url in lot.image_urls)

    numbers = [item.lot_no for item in detail.lots]
    assert numbers == ["Lot.1", "Lot.2", "Lot.3"]


def test_parse_session_detail_falls_back_to_list_card_for_missing_fields():
    """详情页没有围观数（`<span id="pv">` 由 JS 填），必须用列表页那张卡兜底。"""
    card = mxiqi.parse_sessions(AUCTION_HTML)[0]

    detail = mxiqi.parse_session_detail(SESSION_HTML, "327686", fallback=card)

    assert detail.card.view_count == 3982  # 来自列表页
    assert len(detail.card.cover_urls) == 4  # 来自列表页


# ---------------------------------------------------------------- 成交条目（真实 DOM）


def test_parse_sold_items_from_real_html():
    items = mxiqi.parse_sold_items(SOLD_HTML)

    assert [item_id for item_id, _ in items] == ["1870684", "4044197"]
    assert "英国1919年法新" in items[0][1]


def test_real_sold_page_without_login_asks_for_login():
    """真实样本是「未登录」状态：应判为需要登录，且解析不出价格。"""
    assert mxiqi.sold_page_needs_login(SOLD_HTML) is True
    assert mxiqi.parse_sold(SOLD_HTML) == []


def test_parse_sold_reads_prices_filled_by_the_page():
    """登录后页面会把价格填进 `.sold-price` —— 用注入价格后的真实样本验证读取。"""
    import re

    filled = re.sub(
        r'(item_id="1870684"[^>]*>)登录后查看成交价', r"\1¥123 <span>成交</span>", SOLD_HTML
    )
    assert mxiqi.sold_page_needs_login(filled) is True  # 另一条仍是未登录态

    samples = mxiqi.parse_sold(filled)

    assert [sample.price for sample in samples] == [123.0]
    assert "英国1919年法新" in samples[0].title


# ---------------------------------------------------------------- 管道（对象替身）
# 这一段验的是「编排 + 落库」，与 DOM 无关，所以替身直接返回对象而不是解析 HTML。


def _card(mxiqi_id: str = "328062") -> SessionCard:
    return SessionCard(
        mxiqi_id=mxiqi_id,
        title="东印度硬币专场",
        status="running",
        auction_at_raw="10月8日 星期四 15:25",
        lot_total=50,
        view_count=79,
        cover_urls=[f"https://oss.mxiqi.com/V1/AUC/c{i}/M" for i in range(4)],
        shop=ShopInfo(
            mxiqi_uid="49528",
            name="猩哥外币小铺",
            avatar_url="https://oss.mxiqi.com/V1/USR/avatar",
            badges=[{"kind": "认证", "label": "认证"}, {"kind": "years", "label": "2年店铺"}],
        ),
    )


def _detail(mxiqi_id: str) -> SessionDetail:
    card = _card(mxiqi_id)
    return SessionDetail(
        card=card,
        deposit=10.0,
        lots=[
            LotInfo(
                mxiqi_id="1870684",
                lot_no="Lot.1",
                title="东印度公司1808年硬币",
                start_price=15.0,
                current_price=0.0,
                image_urls=[f"https://oss.mxiqi.com/V1/AUC/l{i}/M" for i in range(3)],
                tags={"发行年份": "1808"},
            )
        ],
    )


class _FakeMxiqi:
    """替身：接口与 `MxiqiClient` 一致，但不碰浏览器。"""

    def __init__(self, *, blocked: bool = False, sold: list | None = None) -> None:
        self.blocked = blocked
        self._sold = sold or []
        self.detail_calls: list[str] = []
        self.sold_calls: list[str] = []

    async def list_sessions(self) -> list[SessionCard]:
        if self.blocked:
            raise BlockedError("被反爬拦下")
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


@pytest.mark.asyncio
async def test_pipeline_collects_and_matches(temp_db):
    _seed_tags()
    cfg = config_module.load()
    client = _FakeMxiqi()

    result = await coins_run(cfg, client=client, collect_only=True)

    assert result.status == CoinFetchStatus.OK
    assert result.sessions_stored == 2  # 两个专场用的是同一个替身详情
    # 拍品去重键是「专场 id:站内拍品 id」，所以两条不会互相覆盖
    assert result.lots_stored == 2
    assert result.lots_recommended == 2

    cards = repository.list_coin_session_cards()
    assert len(cards) == 2
    assert all(card.recommended_count == 1 for card in cards)
    assert all(card.view_count == 79 for card in cards)  # 来自列表页那张卡


@pytest.mark.asyncio
async def test_pipeline_prices_and_records_evidence(temp_db):
    from app.coins.pricing import SoldSample

    _seed_tags()
    cfg = _cfg()
    client = _FakeMxiqi(
        sold=[SoldSample("东印度1808", 18), SoldSample("东印度1808", 22), SoldSample("东印度1808", 30)]
    )

    result = await coins_run(cfg, client=client)

    assert result.lots_priced == 2
    assert client.sold_calls, "应该至少查了一次成交"
    lot = next(lot for lot in repository.list_coin_lots_for_matching() if lot.is_recommended)
    ref = repository.get_coin_price_ref(lot.id)
    assert ref is not None
    assert ref.sample_count == 3
    assert ref.median == 22.0  # 中位数
    assert "东印度1808" in ref.raw


@pytest.mark.asyncio
async def test_pipeline_blocked_records_and_stops(temp_db):
    _seed_tags()
    cfg = config_module.load()
    client = _FakeMxiqi(blocked=True)

    result = await coins_run(cfg, client=client)

    assert result.status == CoinFetchStatus.BLOCKED
    assert client.detail_calls == []  # 被拦后不再继续请求
    summary = repository.coin_fetch_summary()
    assert summary["blocked"] is True
    assert summary["last_status"] == "blocked"


@pytest.mark.asyncio
async def test_pipeline_price_respects_per_run_cap(temp_db):
    """单轮查价有上限（拿登录账号在查，宁可慢也别被封）；超出的留到下一轮补。"""
    from app.coins.pricing import SoldSample

    _seed_tags()
    cfg = _cfg(max_queries_per_run=1)
    client = _FakeMxiqi(
        sold=[SoldSample("东印度1808", 18), SoldSample("东印度1808", 22), SoldSample("东印度1808", 30)]
    )

    first = await coins_run(cfg, client=client)

    assert first.lots_priced == 1  # 上限 1 条
    assert first.lots_pricing_deferred == 1  # 另一条推迟
    assert len(client.sold_calls) == 1

    second = await coins_run(cfg, client=client, price_only=True)

    assert second.lots_priced == 1  # 上轮推迟的这条这轮补上
    assert second.lots_pricing_skipped == 1  # 上轮查过的当日不再查


@pytest.mark.asyncio
async def test_pipeline_skips_pricing_when_keyword_too_generic(temp_db):
    """命中里没有面值/品类 → 关键词太泛，**主动不报价**（否则会报出离谱的价）。

    实测背景：当前价 ¥330 的「巴拿马1967年精制套币」被关键词「巴拿马 1967」报成 ¥2。
    """
    from app.coins.pricing import SoldSample

    # 只给国家 + 年代：总分 2.0 够推荐，但拼出来的关键词太泛
    repository.upsert_coin_tag("country", "东印度", 1.0, 4)
    repository.upsert_coin_tag("era", "1808", 1.0, 4)
    cfg = _cfg()
    client = _FakeMxiqi(
        sold=[SoldSample("东印度1808", 18), SoldSample("东印度1808", 22), SoldSample("东印度1808", 30)]
    )

    result = await coins_run(cfg, client=client)

    assert result.lots_recommended == 2
    assert result.lots_priced == 0
    assert result.lots_pricing_too_generic == 2
    assert client.sold_calls == []  # 压根没发请求


@pytest.mark.asyncio
async def test_pipeline_price_skips_already_priced_today(temp_db):
    """当日已查过价的拍品不再重复请求（也是限速的一部分）。"""
    from app.coins.pricing import SoldSample

    _seed_tags()
    cfg = _cfg()
    client = _FakeMxiqi(
        sold=[SoldSample("东印度1808", 18), SoldSample("东印度1808", 22), SoldSample("东印度1808", 30)]
    )

    first = await coins_run(cfg, client=client)
    calls_after_first = len(client.sold_calls)

    second = await coins_run(cfg, client=client, price_only=True)

    assert len(client.sold_calls) == calls_after_first  # 当日已查过 → 不重复请求
    assert second.lots_pricing_skipped == first.lots_priced

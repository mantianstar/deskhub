"""推荐价统计测试（M9-2，plan §18.11）。离线，验证 spec §14.6 的两条红线。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.coins import pricing
from app.coins.pricing import SoldSample
from app.models import CoinPriceRef, CoinSession


def _at(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 统计


def test_summarize_median_and_quartiles():
    samples = [SoldSample("a", 10), SoldSample("b", 20), SoldSample("c", 30),
               SoldSample("d", 40), SoldSample("e", 50)]

    summary = pricing.summarize(samples, statistic="median", min_samples=3)

    assert (summary.sample_count, summary.p25, summary.point, summary.p75) == (5, 20.0, 30.0, 40.0)


def test_summarize_mean_statistic():
    samples = [SoldSample("a", 10), SoldSample("b", 20), SoldSample("c", 30)]
    summary = pricing.summarize(samples, statistic="mean", min_samples=3)
    assert summary.point == 20.0


def test_summarize_below_min_samples_gives_range_without_point():
    """红线 1：样本 < 3 只给区间，不给单一价（spec §14.6）。"""
    summary = pricing.summarize([SoldSample("a", 10), SoldSample("b", 20)], min_samples=3)

    assert summary.sample_count == 2
    assert summary.point is None
    assert summary.p25 == 12.5
    assert summary.p75 == 17.5


def test_summarize_zero_samples_gives_nothing():
    """红线 2：无样本时绝不给价（spec §14.6）。"""
    summary = pricing.summarize([], min_samples=3)
    assert (summary.sample_count, summary.p25, summary.point, summary.p75) == (0, None, None, None)


def test_summarize_single_sample_with_min_one():
    summary = pricing.summarize([SoldSample("a", 7)], min_samples=1)
    assert (summary.sample_count, summary.point, summary.p25, summary.p75) == (1, 7.0, 7.0, 7.0)


def test_summarize_ignores_negative_prices():
    summary = pricing.summarize([SoldSample("a", -1), SoldSample("b", 10), SoldSample("c", 20)],
                                min_samples=2)
    assert summary.sample_count == 2


# ---------------------------------------------------------------- 到手价（plan §18.18.1 P1-8）


def _session(**overrides) -> CoinSession:
    base = dict(
        id=None,
        mxiqi_id="1",
        shop_id=None,
        title="场次",
        status="closed",
        auction_at=None,
        auction_at_raw=None,
        deposit=None,
        lot_total=None,
        view_count=None,
        cover_urls=[],
        fetched_at="2026-10-22T00:00:00Z",
    )
    return CoinSession(**base, **overrides)


def test_landed_price_zero_rate_equals_price_plus_shipping():
    """验收：commission_rate=0 时到手价 = 成交价 + 运费。"""
    session = _session(commission_rate=0.0, commission_min=0.0, express_fee=8.0)
    assert pricing.landed_price(258.0, session) == 266.0


def test_landed_price_applies_rate():
    """验收：费率 0.03 → 成交价 × 1.03 + 运费。"""
    session = _session(commission_rate=0.03, commission_min=0.0, express_fee=8.0)
    assert pricing.landed_price(100.0, session) == 111.0


def test_landed_price_uses_commission_min_floor():
    """费率算出来低于最低佣金时，按最低佣金收。"""
    session = _session(commission_rate=0.03, commission_min=5.0, express_fee=8.0)
    assert pricing.landed_price(100.0, session) == 113.0


def test_landed_price_free_shipping_over_threshold():
    """成交价达到包邮门槛时不加运费。"""
    session = _session(commission_rate=0.0, express_fee=8.0, express_free_amount=100.0)
    assert pricing.landed_price(100.0, session) == 100.0
    assert pricing.landed_price(99.0, session) == 107.0


def test_landed_price_without_fee_fields_equals_final_price():
    """老场次没有费率 / 运费字段时，到手价等于成交价。"""
    assert pricing.landed_price(58.0, _session()) == 58.0


# ---------------------------------------------------------------- 时间窗口


def test_within_window_filters_old_but_keeps_missing_date():
    assert pricing._within_window(SoldSample("old", 1, _at(400)), 180) is False
    assert pricing._within_window(SoldSample("new", 1, _at(10)), 180) is True
    assert pricing._within_window(SoldSample("nodate", 1, None), 180) is True


# ---------------------------------------------------------------- price_lot


class _FakeSource:
    def __init__(self, samples: list[SoldSample]) -> None:
        self._samples = samples
        self.calls: list[str] = []

    async def search_sold(self, keyword: str) -> list[SoldSample]:
        self.calls.append(keyword)
        return self._samples


@pytest.mark.asyncio
async def test_price_lot_builds_ref_with_evidence():
    source = _FakeSource([SoldSample("东印度1808", 18), SoldSample("东印度1808", 20),
                          SoldSample("东印度1808", 30)])

    ref = await pricing.price_lot(source, lot_id=7, keyword="东印度 1808", min_samples=3)

    assert source.calls == ["东印度 1808"]
    assert ref.lot_id == 7
    assert ref.keyword == "东印度 1808"
    assert ref.sample_count == 3
    assert ref.median == 20.0
    assert "东印度1808" in ref.raw  # 取证：命中的成交明细落库


@pytest.mark.asyncio
async def test_price_lot_empty_keyword_skips_network():
    source = _FakeSource([SoldSample("x", 1)])
    ref = await pricing.price_lot(source, lot_id=1, keyword="")
    assert source.calls == []  # 没关键词就不请求
    assert ref.sample_count == 0
    assert ref.median is None


@pytest.mark.asyncio
async def test_price_lot_insufficient_samples_has_no_point():
    source = _FakeSource([SoldSample("x", 15)])
    ref = await pricing.price_lot(source, lot_id=1, keyword="英国 1919", min_samples=3)
    assert ref.sample_count == 1
    assert ref.median is None
    assert ref.p25 == 15.0


# ---------------------------------------------------------------- 成交明细（plan/coins.md §18.18 P0-3）


def test_sales_from_ref_skips_rows_without_item_id():
    """旧取证只有标题 + 价格，无法按 item_id 去重 → 跳过，不污染成交库。"""
    ref = CoinPriceRef(
        lot_id=1,
        keyword="东印度 1808",
        sample_count=2,
        p25=18.0,
        median=19.0,
        p75=20.0,
        raw=json.dumps(
            [
                {"item_id": "9001", "title": "东印度1808", "price": 18.0, "sold_at": None},
                {"title": "东印度1808", "price": 20.0},
            ],
            ensure_ascii=False,
        ),
        queried_at="2026-10-21T00:00:00Z",
    )

    sales = pricing.sales_from_ref(ref)

    assert [(sale.item_id, sale.source, sale.keyword) for sale in sales] == [
        ("9001", "sold_search", "东印度 1808")
    ]

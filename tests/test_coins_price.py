"""推荐价统计测试（M9-2，plan §18.11）。离线，验证 spec §14.6 的两条红线。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.coins import pricing
from app.coins.pricing import SoldSample


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

"""推荐价：站内历史成交 → 中心价 + P25–P75 区间（spec §14.6 / plan §18.6）。

红线（spec §14.6）：
- 样本 < `min_samples` → 只给区间，**不给单一价**。
- 0 样本 → 不写任何价格点估计，只在页面上标「无成交样本」。

取数来源是麦稀奇**站内历史成交**（不是我个人成交），由 `gapi.MxiqiGapi.search_sold` 提供；
这里只做「过滤 + 统计」，不感知站点细节，所以能离线单测。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app import repository
from app.models import CoinPriceRef


@dataclass(frozen=True)
class SoldSample:
    """一条历史成交样本（`sold_at` 可能缺失，站点不一定给日期）。"""

    title: str
    price: float
    sold_at: str | None = None


class PriceSource(Protocol):
    async def search_sold(self, keyword: str) -> list[SoldSample]: ...


@dataclass(frozen=True)
class PriceSummary:
    """统计结果。`point` 是中心价（`median` 或 `mean`）；样本不足时为 None。"""

    sample_count: int
    p25: float | None
    point: float | None
    p75: float | None
    statistic: str


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """线性插值分位（样本很少也能算，不像 `statistics.quantiles` 有最小样本限制）。"""
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def summarize(
    samples: Sequence[SoldSample],
    *,
    statistic: str = "median",
    min_samples: int = 3,
) -> PriceSummary:
    """把成交样本压成「中心价 + 区间 + 样本数」。样本不足时中心价为 None。"""
    prices = sorted(sample.price for sample in samples if sample.price is not None and sample.price >= 0)
    count = len(prices)
    if count == 0:
        return PriceSummary(0, None, None, None, statistic)

    p25 = round(_percentile(prices, 0.25), 2)
    p75 = round(_percentile(prices, 0.75), 2)
    point = _percentile(prices, 0.5) if statistic == "median" else sum(prices) / count
    if count < min_samples:
        # 采样太少 → 不给单一价，只给区间（spec §14.6 红线）
        return PriceSummary(count, p25, None, p75, statistic)
    return PriceSummary(count, p25, round(point, 2), p75, statistic)


def _within_window(sample: SoldSample, window_days: int) -> bool:
    """按成交日期过滤；样本没有日期时**保留**（宁可多给样本，也不因缺日期丢证据）。"""
    if not sample.sold_at:
        return True
    parsed = repository.parse_dt(sample.sold_at)
    if parsed is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    return parsed >= cutoff


async def price_lot(
    source: PriceSource,
    lot_id: int,
    keyword: str,
    *,
    statistic: str = "median",
    min_samples: int = 3,
    window_days: int = 180,
) -> CoinPriceRef:
    """查一个关键词的成交、统计、组装成可落库的 `CoinPriceRef`。

    `keyword` 为空时直接返回 0 样本的记录（不请求站内搜索，省一次往返）。
    """
    samples: list[SoldSample] = []
    if keyword:
        fetched = await source.search_sold(keyword)
        samples = [sample for sample in fetched if _within_window(sample, window_days)]

    summary = summarize(samples, statistic=statistic, min_samples=min_samples)
    raw = json.dumps(
        [{"title": sample.title, "price": sample.price} for sample in samples],
        ensure_ascii=False,
    )
    return CoinPriceRef(
        lot_id=lot_id,
        keyword=keyword,
        sample_count=summary.sample_count,
        p25=summary.p25,
        # 列名沿用 `median`：v1 默认口径就是中位数；换 mean 时这里存的是均值
        median=summary.point,
        p75=summary.p75,
        raw=raw,
        queried_at=repository.utcnow(),
    )

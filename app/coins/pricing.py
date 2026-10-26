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
from app.models import CoinPriceRef, CoinSale, CoinSession


@dataclass(frozen=True)
class SoldSample:
    """一条历史成交样本（`sold_at` 可能缺失，站点不一定给日期）。"""

    title: str
    price: float
    sold_at: str | None = None
    # 站内拍品 id：成交明细表按它去重（plan/coins.md §18.18 P0-3）
    item_id: str | None = None
    # 热度：`home.filter` 才有；缺键为 None（不当 0，plan §18.18.1 P1-6）
    bid_num: int | None = None
    bid_user_num: int | None = None
    # 站点分类（最细一级；plan §18.19.4）；`coin_sales` 不落这列，只留在取证 raw 里
    category_code: str | None = None


class PriceSource(Protocol):
    async def search_sold(
        self, keyword: str, cate_id_1: int | None = None, page: int = 1
    ) -> list[SoldSample]: ...


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


def landed_price(final_price: float, session: CoinSession) -> float:
    """成交价 → 到手价：成交价 + 佣金（费率与最低佣金取高）+ 运费（达包邮门槛免运费）。

    保险费、汇率与地区运费差异不计（plan/coins.md §18.18.1 P1-8）；
    老场次没有费率 / 运费字段时按 0 处理，到手价等于成交价。
    """
    rate = session.commission_rate or 0.0
    floor = session.commission_min or 0.0
    commission = max(final_price * rate, floor)
    threshold = session.express_free_amount
    fee = session.express_fee or 0.0
    shipping = 0.0 if threshold is not None and final_price >= threshold else fee
    return round(final_price + commission + shipping, 2)


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
        [
            {
                "item_id": sample.item_id,
                "title": sample.title,
                "price": sample.price,
                "sold_at": sample.sold_at,
                "bid_num": sample.bid_num,
                "bid_user_num": sample.bid_user_num,
                "category_code": sample.category_code,
            }
            for sample in samples
        ],
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


def bid_updates_from_ref(ref: CoinPriceRef) -> list[tuple[str, int | None, int | None]]:
    """`coin_price_refs.raw` → `(item_id, bid_num, bid_user_num)`，补拍品热度用。

    只有带热度的条目才返回；`item_id` 是 `coin_lots.mxiqi_id` 冒号后的那一段。
    """
    if not ref.raw:
        return []
    try:
        rows = json.loads(ref.raw)
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []

    updates: list[tuple[str, int | None, int | None]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("item_id") or "").strip()
        bid_num = row.get("bid_num")
        bid_user_num = row.get("bid_user_num")
        if not item_id or (bid_num is None and bid_user_num is None):
            continue
        updates.append((item_id, bid_num, bid_user_num))
    return updates


def sales_from_ref(ref: CoinPriceRef) -> list[CoinSale]:
    """`coin_price_refs.raw` → 可落库的成交行（`source=sold_search`）。

    缺 `item_id` 的旧证据跳过（那批 raw 只有标题 + 价格，无法去重）。
    """
    if not ref.raw:
        return []
    try:
        rows = json.loads(ref.raw)
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []

    now = repository.utcnow()
    sales: list[CoinSale] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("item_id") or "").strip()
        title = str(row.get("title") or "").strip()
        price = row.get("price")
        if not item_id or not title or price is None:
            continue
        sales.append(
            CoinSale(
                id=None,
                item_id=item_id,
                title=title,
                price=float(price),
                sold_at=row.get("sold_at"),
                session_id=None,
                shop_id=None,
                keyword=ref.keyword,
                source="sold_search",
                fetched_at=now,
            )
        )
    return sales

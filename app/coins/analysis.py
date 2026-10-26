"""分析页统计（plan/coins.md §18.19.7）：把筛选后的拍品行压成各 Tab 需要的分组指标。

纯函数：只吃 `repository.list_coin_lot_analysis_rows` 的行，不碰数据库、不发请求。
口径（spec §14.19.4）：
- 成交率 = 成交 /（成交 + 流拍），**不含在拍**；
- 价格一律用中位数并附样本数；
- 样本数 < `MIN_SAMPLES` 的分组标「样本不足」，不给结论。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

MIN_SAMPLES = 5

# 价格带 / 起拍价档位（元）：左闭右开，最后一档无上界
PRICE_BANDS: list[tuple[float, float | None, str]] = [
    (0, 50, "< ¥50"),
    (50, 200, "¥50–200"),
    (200, 500, "¥200–500"),
    (500, 2000, "¥500–2000"),
    (2000, None, "≥ ¥2000"),
]

# 错标抽查：分类 → 与标题冲突的关键词（只展示，不回写分类；spec §14.19.2）
MISLABEL_RULES: dict[str, tuple[str, ...]] = {
    "11040": ("银元", "银币", "金币", "银子"),
    "11050": ("铜元", "铜币", "铜板", "金币"),
    "11060": ("铜元", "铜币", "银元", "银币"),
}

_TITLE_NOISE_RE = re.compile(r"[\s\W_]+")


@dataclass(frozen=True)
class AnalysisReport:
    totals: dict[str, int]
    overview: list[dict]
    prices: list[dict]
    start_bands: list[dict]
    shops: list[dict]
    relist: dict[str, int]
    mislabels: list[dict]
    band_labels: list[str] = field(default_factory=lambda: [label for *_rest, label in PRICE_BANDS])


def _percentile(sorted_values: list[float], q: float) -> float:
    """线性插值分位（样本少也能算）。"""
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def _summarize(prices: list[float], samples: int) -> dict:
    """价格摘要：样本不足时不给分位数（页面显示占位）。"""
    values = sorted(price for price in prices if price is not None and price >= 0)
    count = len(values)
    if samples < MIN_SAMPLES or count == 0:
        return {"count": count, "p25": None, "p50": None, "p75": None, "insufficient": True}
    return {
        "count": count,
        "p25": round(_percentile(values, 0.25), 2),
        "p50": round(_percentile(values, 0.5), 2),
        "p75": round(_percentile(values, 0.75), 2),
        "insufficient": count < MIN_SAMPLES,
    }


def _band_for(value: float | None) -> str | None:
    if value is None or value < 0:
        return None
    for low, high, label in PRICE_BANDS:
        if value >= low and (high is None or value < high):
            return label
    return None


def _norm_title(title: str | None) -> str:
    return _TITLE_NOISE_RE.sub("", (title or "").lower())


def _rate(sold: int, unsold: int) -> str:
    denominator = sold + unsold
    if denominator == 0:
        return "—"
    return f"{sold / denominator:.1%}"


def _is_sold(row) -> bool:
    return row["outcome"] == "sold" and row["final_price"] is not None


def _is_unsold(row) -> bool:
    return row["outcome"] == "unsold"


def build_report(rows) -> AnalysisReport:
    """行 → 各 Tab 的分组指标。行已由 SQL 过滤为「硬币 + 裸币」。"""
    totals = {
        "lots": len(rows),
        "sold": sum(1 for row in rows if _is_sold(row)),
        "unsold": sum(1 for row in rows if _is_unsold(row)),
        "running": sum(1 for row in rows if not _is_sold(row) and not _is_unsold(row)),
    }
    return AnalysisReport(
        totals=totals,
        overview=_overview(rows),
        prices=_prices(rows),
        start_bands=_start_bands(rows),
        shops=_shops(rows),
        relist=_relist(rows),
        mislabels=_mislabels(rows),
    )


def _overview(rows) -> list[dict]:
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[row["category_code"] or "未分类"].append(row)
    overview = []
    for code, group in groups.items():
        sold = [row for row in group if _is_sold(row)]
        unsold = [row for row in group if _is_unsold(row)]
        samples = len(sold) + len(unsold)
        summary = _summarize([row["final_price"] for row in sold], samples)
        overview.append(
            {
                "code": code,
                "name": group[0]["category_name"],
                "lots": len(group),
                "sold": len(sold),
                "unsold": len(unsold),
                "samples": samples,
                "rate": _rate(len(sold), len(unsold)),
                "median": summary["p50"],
                "price_count": summary["count"],
                "insufficient": samples < MIN_SAMPLES,
            }
        )
    return sorted(overview, key=lambda item: (-item["lots"], item["code"]))


def _prices(rows) -> list[dict]:
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[row["category_code"] or "未分类"].append(row)
    bands = [label for *_rest, label in PRICE_BANDS]
    result = []
    for code, group in groups.items():
        sold = [row for row in group if _is_sold(row)]
        if not sold:
            continue
        summary = _summarize([row["final_price"] for row in sold], len(sold))
        distribution = {label: 0 for label in bands}
        for row in sold:
            label = _band_for(row["final_price"])
            if label:
                distribution[label] += 1
        ratios = [
            row["final_price"] / row["numista_value"]
            for row in sold
            if row["numista_value"] and row["numista_value"] > 0 and row["final_price"] > 0
        ]
        result.append(
            {
                "code": code,
                "name": group[0]["category_name"],
                "distribution": [distribution[label] for label in bands],
                "p25": summary["p25"],
                "p50": summary["p50"],
                "p75": summary["p75"],
                "price_count": summary["count"],
                "insufficient": summary["insufficient"],
                "numista_count": len(ratios),
                "numista_coverage": f"{len(ratios) / len(sold):.1%}",
                "numista_ratio": round(_percentile(sorted(ratios), 0.5), 2) if ratios else None,
            }
        )
    return sorted(result, key=lambda item: (-item["price_count"], item["code"]))


def _start_bands(rows) -> list[dict]:
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        label = _band_for(row["start_price"])
        if label:
            groups[label].append(row)
    result = []
    for _low, _high, label in PRICE_BANDS:
        group = groups.get(label, [])
        sold = sum(1 for row in group if _is_sold(row))
        unsold = sum(1 for row in group if _is_unsold(row))
        result.append(
            {
                "label": label,
                "lots": len(group),
                "sold": sold,
                "unsold": unsold,
                "rate": _rate(sold, unsold),
                "insufficient": sold + unsold < MIN_SAMPLES,
            }
        )
    return result


def _shops(rows) -> list[dict]:
    groups: dict[tuple[int | None, str], list] = defaultdict(list)
    for row in rows:
        groups[(row["shop_id"], row["shop_name"] or "未知店铺")].append(row)
    result = []
    for (shop_id, shop_name), group in groups.items():
        sold = [row for row in group if _is_sold(row)]
        unsold = sum(1 for row in group if _is_unsold(row))
        summary = _summarize([row["final_price"] for row in sold], len(sold) + unsold)
        title_counts: dict[str, int] = defaultdict(int)
        for row in group:
            title_counts[_norm_title(row["title"])] += 1
        repeated = sum(
            1 for row in group if _norm_title(row["title"]) and title_counts[_norm_title(row["title"])] > 1
        )
        result.append(
            {
                "shop_id": shop_id,
                "name": shop_name,
                "lots": len(group),
                "sold": len(sold),
                "unsold": unsold,
                "rate": _rate(len(sold), unsold),
                "median": summary["p50"],
                "price_count": summary["count"],
                "repeat_rate": f"{repeated / len(group):.1%}" if group else "—",
                "insufficient": len(sold) + unsold < MIN_SAMPLES,
            }
        )
    return sorted(result, key=lambda item: (-item["lots"], item["name"]))


def _relist(rows) -> dict[str, int]:
    """流拍后再上架：按归一化标题找重复出现；流拍条目后面还有同标题拍品即算复现。

    同图复现的准确口径要排除描述图，这里先用标题做近似（页面已标注口径）。
    """
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        key = _norm_title(row["title"])
        if key:
            groups[key].append(row)
    repeat_groups = 0
    unsold_relisted = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        repeat_groups += 1
        ordered = sorted(
            group,
            key=lambda row: (row["auction_at"] or row["closed_at"] or "", row["session_id"]),
        )
        for index, row in enumerate(ordered):
            if _is_unsold(row) and len(ordered) - index > 1:
                unsold_relisted += 1
    return {
        "repeat_groups": repeat_groups,
        "unsold_relisted": unsold_relisted,
        "lots": len(rows),
    }


def _mislabels(rows) -> list[dict]:
    samples = []
    for row in rows:
        keywords = MISLABEL_RULES.get(row["category_code"] or "")
        if not keywords:
            continue
        title = row["title"] or ""
        matched = next((word for word in keywords if word in title), None)
        if matched:
            samples.append(
                {
                    "title": title,
                    "category_name": row["category_name"],
                    "keyword": matched,
                    "outcome": row["outcome"],
                }
            )
        if len(samples) >= 10:
            break
    return samples

"""店铺分析（handoff 2026-11-03 / 2026-11-04）：拍品级明细 → 店铺榜单 + 五维评分。

纯函数：只吃 `repository.list_coin_shop_analysis_rows` 的行与当日场次，不碰数据库。
口径见原型 `data/prototypes/shop-analysis.html`「口径说明」：
- 成交率 = 成交 /（成交 + 流拍），不含在拍；价格一律用中位数；
- 有效样本 < `MIN_SAMPLES` 不给总分，只标「样本不足」；
- 出价类指标只统计有 `bid_user_num` 的样本，缺失按可得权重归一，不扣分。
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from app.coins.analysis import MIN_SAMPLES, _norm_title, _percentile

# 五维权重（原型「口径说明」最后一条）
WEIGHTS: dict[str, int] = {"trust": 30, "heat": 20, "price": 20, "delivery": 20, "sample": 10}

RISK_RED = 3  # 同图重挂 ≥3 次标红
BID_COVERAGE_LOW = 0.3
TEMPLATE_IMAGE_LOTS = 10
TEMPLATE_IMAGE_SHOPS = 5
PRICE_TOLERANCE = 1.5  # 同标题价差 |gap| ≥1.5 时价格分归零
CONCENTRATION_SHARE = 0.6
SHILL_MIN_SESSIONS = 10
SHILL_MAX_WIN_RATE = 0.1
SHILL_MIN_BID_SHARE = 0.2

SESSION_STATUS_LABELS = {"running": "在拍", "preview": "预告", "closed": "已结束"}


@dataclass(frozen=True)
class ShopReport:
    shops: list[dict]
    totals: dict[str, int]


def build_shop_report(rows, today_sessions, *, min_samples: int = MIN_SAMPLES) -> ShopReport:
    """按店铺聚合；`today_sessions` 是 `{shop_id: [场次行]}`，用于「前往今日拍场」。"""
    all_rows = list(rows)
    grouped: dict[int | None, list] = defaultdict(list)
    for row in all_rows:
        grouped[row["shop_id"]].append(row)
    title_base = _title_baseline(all_rows, min_samples)
    category_base = _category_baseline(all_rows, min_samples)
    image_stats = _image_stats(all_rows)
    shops = [
        _shop_metrics(
            shop_id,
            group,
            (today_sessions or {}).get(shop_id),
            title_base,
            category_base,
            image_stats,
            min_samples,
        )
        for shop_id, group in grouped.items()
    ]
    totals = {
        "shops": len(shops),
        "samples": sum(shop["samples"] for shop in shops),
        "sufficient": sum(1 for shop in shops if not shop["insufficient"]),
        "risk": sum(1 for shop in shops if shop["recheck"]),
    }
    return ShopReport(shops=shops, totals=totals)


# ---------------------------------------------------------------- 聚合


def _shop_metrics(
    shop_id: int | None,
    group: list,
    sessions: list | None,
    title_base: dict[str, float],
    category_base: dict[str, float],
    image_stats: dict[str, tuple[int, int]],
    min_samples: int,
) -> dict:
    ordered = sorted(group, key=lambda row: (row["auction_at"] or "", row["id"]))
    sold = [row for row in ordered if _is_sold(row)]
    unsold = [row for row in ordered if row["outcome"] == "unsold"]
    samples = len(sold) + len(unsold)
    rate = len(sold) / samples if samples else None

    bid_rows = [row for row in ordered if row["bid_user_num"] is not None]
    coverage = len(bid_rows) / len(ordered) if ordered else 0.0
    sold_with_bid = [row for row in sold if row["bid_user_num"] is not None]
    real_bid = (
        sum(1 for row in sold_with_bid if row["bid_user_num"] >= 2) / len(sold_with_bid)
        if sold_with_bid
        else None
    )
    bidders, bids = _session_bid_stats(ordered)
    watch_ratio = _watch_ratio(ordered)
    gap, gap_count = _title_gap(sold, title_base)
    category_gap = _category_gap(sold, category_base)
    relist = _relist_count(ordered, image_stats)
    delivery = _delivery(ordered)
    concentrated = _bidder_concentration(ordered, min_samples)
    shill = _shill_flag(ordered)
    today = _pick_today_session(sessions)
    zero_start = (
        sum(1 for row in ordered if row["start_price"] == 0) / len(ordered) if ordered else 0.0
    )

    scores = {
        "trust": _trust_score(rate, real_bid),
        "heat": _heat_score(bidders, watch_ratio),
        "price": _price_score(gap),
        "delivery": _delivery_score(delivery),
        "sample": _sample_score(samples, min_samples),
    }
    total = _total_score(scores)
    tags = _risk_tags(
        relist=relist,
        concentrated=concentrated,
        shill=shill,
        coverage=coverage,
        samples=samples,
        gap_count=gap_count,
        delivery=delivery,
        min_samples=min_samples,
    )
    latest = ordered[-1]
    heat = _heat_detail(ordered, coverage, bidders, bids)

    return {
        "shop_id": shop_id,
        "name": latest["shop_name"] or "未知店铺",
        "followed": bool(latest["shop_followed"]),
        "sub": f"{len({row['session_id'] for row in ordered})} 场 · {len(ordered)} 件拍品",
        "samples": samples,
        "rate": rate,
        "gap": gap,
        "lots": len(ordered),
        "insufficient": samples < min_samples,
        "score": total,
        "score_text": _score_text(total),
        "score_class": _score_class(total),
        "rate_text": _fmt_pct(rate),
        "real_bid_text": _fmt_pct(real_bid),
        "coverage_text": f"覆盖 {coverage:.0%}" if bid_rows else "",
        "bidders_text": heat["bidders"],
        "watch_text": _fmt_pct(watch_ratio, 1),
        "delivery_text": _delivery_text(delivery),
        "delivery_sub": _delivery_sub(delivery),
        "tags": tags,
        "recheck": any(tag["tone"] in ("red", "yellow") and tag["label"] != "样本不足" for tag in tags),
        "ratings": [
            {
                "label": label,
                "weight": WEIGHTS[key],
                "value": None if scores[key] is None else round(scores[key]),
                "text": _score_text(scores[key]),
                "xy": _radar_xy(scores[key], index),
            }
            for index, (key, label) in enumerate((
                ("trust", "成交真实度"),
                ("heat", "竞争热度"),
                ("price", "价格合理"),
                ("delivery", "交付条件"),
                ("sample", "样本可信"),
            ))
        ],
        "rating_note": (
            "有效样本不足，不给加权总分"
            if total is None
            else f"加权总分 {round(total)} ｜ 无出价数据的维度按可得权重归一"
        ),
        "facts": [
            {"label": "同标题价差", "value": f"{_fmt_gap(gap)}（可比 {gap_count} 条）"},
            {"label": "同分类价差", "value": _fmt_gap(category_gap)},
            {"label": "数据质量", "value": f"起拍价为 0 占 {zero_start:.0%}"},
        ],
        "samples_list": _recent_samples(sold),
        "heat": heat,
        "verdict": _verdict(tags, samples, min_samples),
        "today_session": today,
    }


def _title_baseline(rows, min_samples: int) -> dict[str, float]:
    """全平台同标题成交中位数（样本 < min_samples 不入基准）。"""
    prices: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if _is_sold(row):
            key = _norm_title(row["title"])
            if key:
                prices[key].append(row["final_price"])
    return {
        key: _percentile(sorted(values), 0.5)
        for key, values in prices.items()
        if len(values) >= min_samples
    }


def _category_baseline(rows, min_samples: int) -> dict[str, float]:
    prices: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if _is_sold(row) and row["category_code"]:
            prices[row["category_code"]].append(row["final_price"])
    return {
        code: _percentile(sorted(values), 0.5)
        for code, values in prices.items()
        if len(values) >= min_samples
    }


def _image_stats(rows) -> dict[str, tuple[int, int]]:
    """oss_key → (全平台出现条数, 覆盖店铺数)，用于剔除店铺模板图。"""
    counts: dict[str, int] = defaultdict(int)
    shops: dict[str, set] = defaultdict(set)
    for row in rows:
        for key in _image_keys(row):
            counts[key] += 1
            if row["shop_id"] is not None:
                shops[key].add(row["shop_id"])
    return {key: (counts[key], len(shops[key])) for key in counts}


def _watch_ratio(group) -> float | None:
    """围观→出价：仅已结束场次，出价人数合计 ÷ 围观人数合计（UV）。"""
    sessions: dict[int, dict[str, int]] = {}
    for row in group:
        if row["session_status"] != "closed":
            continue
        entry = sessions.setdefault(
            row["session_id"], {"viewers": row["viewer_count"] or 0, "bidders": 0}
        )
        if row["bid_user_num"] is not None:
            entry["bidders"] += row["bid_user_num"]
    viewers = sum(entry["viewers"] for entry in sessions.values())
    if not viewers:
        return None
    return sum(entry["bidders"] for entry in sessions.values()) / viewers


def _title_gap(sold, base: dict[str, float]) -> tuple[float | None, int]:
    """本店同标题成交中位 ÷ 全平台同标题中位 − 1；返回 (中位价差, 可比条数)。"""
    by_title: dict[str, list[float]] = defaultdict(list)
    for row in sold:
        key = _norm_title(row["title"])
        if key:
            by_title[key].append(row["final_price"])
    gaps: list[float] = []
    comparable = 0
    for key, prices in by_title.items():
        reference = base.get(key)
        if not reference:
            continue
        gaps.append(_percentile(sorted(prices), 0.5) / reference - 1)
        comparable += len(prices)
    if not gaps:
        return None, 0
    return _percentile(sorted(gaps), 0.5), comparable


def _category_gap(sold, base: dict[str, float]) -> float | None:
    by_code: dict[str, list[float]] = defaultdict(list)
    for row in sold:
        if row["category_code"]:
            by_code[row["category_code"]].append(row["final_price"])
    gaps = [
        _percentile(sorted(prices), 0.5) / base[code] - 1
        for code, prices in by_code.items()
        if base.get(code)
    ]
    return _percentile(sorted(gaps), 0.5) if gaps else None


def _relist_count(group, image_stats: dict[str, tuple[int, int]]) -> int:
    """同图重挂：本店流拍拍品（模板图除外）在后续专场再次出现，按拍品计次。"""
    count = 0
    for index, row in enumerate(group):
        if row["outcome"] != "unsold":
            continue
        keys = {
            key
            for key in _image_keys(row)
            if image_stats.get(key, (0, 0))[0] < TEMPLATE_IMAGE_LOTS
            and image_stats.get(key, (0, 0))[1] < TEMPLATE_IMAGE_SHOPS
        }
        if not keys:
            continue
        for other in group[index + 1 :]:
            if other["session_id"] != row["session_id"] and keys & set(_image_keys(other)):
                count += 1
                break
    return count


def _delivery(group) -> dict:
    latest = group[-1]
    return {
        "commission_rate": latest["commission_rate"],
        "commission_min": latest["commission_min"],
        "express_fee": latest["express_fee"],
        "express_day": latest["express_day"],
        "is_storage": bool(latest["is_storage"]),
    }


def _bidder_concentration(group, min_samples: int) -> bool:
    """出价人集中：单一 uid 出现在 ≥60% 的有出价人样本里（样本 ≥ min_samples）。"""
    counts: dict[str, int] = defaultdict(int)
    lots_with_users = 0
    for row in group:
        users = set(_json_list(row["bid_users"]))
        if not users:
            continue
        lots_with_users += 1
        for uid in users:
            counts[uid] += 1
    if lots_with_users < min_samples or not counts:
        return False
    return max(counts.values()) / lots_with_users >= CONCENTRATION_SHARE


def _shill_flag(group) -> bool:
    """疑似托价（参考）：同一 uid 在本店出价 ≥10 场且中签率 ≤10%，其出价占比 ≥20%。"""
    stats: dict[str, dict] = {}
    for row in group:
        users = set(_json_list(row["bid_users"]))
        if not users:
            continue
        for uid in users:
            entry = stats.setdefault(uid, {"sessions": set(), "bids": 0, "wins": 0})
            entry["sessions"].add(row["session_id"])
            entry["bids"] += 1
            if row["outcome"] == "sold" and row["winner_uid"] == uid:
                entry["wins"] += 1
    total_bids = sum(entry["bids"] for entry in stats.values())
    if not total_bids:
        return False
    suspicious = sum(
        entry["bids"]
        for entry in stats.values()
        if len(entry["sessions"]) >= SHILL_MIN_SESSIONS
        and entry["wins"] / len(entry["sessions"]) <= SHILL_MAX_WIN_RATE
    )
    return suspicious / total_bids >= SHILL_MIN_BID_SHARE


def _pick_today_session(sessions: list | None) -> dict | None:
    """当天多场：在拍优先，其次取展示时间最晚的一场。"""
    if not sessions:
        return None
    running = [row for row in sessions if (row["status"] or "") == "running"]
    pool = running or list(sessions)
    best = max(pool, key=lambda row: (row["auction_at"] or "", row["id"]))
    return {
        "id": best["id"],
        "title": best["title"],
        "status_label": SESSION_STATUS_LABELS.get(best["status"] or "", "未知"),
        "auction_at": best["auction_at"] or "",
    }


# ---------------------------------------------------------------- 评分


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _trust_score(rate: float | None, real_bid: float | None) -> float | None:
    if rate is None:
        return None
    base = _clamp(100 * (rate - 0.4) / 0.5)
    if real_bid is None:
        return base
    return _clamp(0.6 * base + 0.4 * _clamp(40 + (real_bid - 0.5) * 100))


def _heat_score(bidders: float | None, watch_ratio: float | None) -> float | None:
    parts: list[float] = []
    if bidders is not None:
        if bidders < 2:
            parts.append(40.0)
        elif bidders < 3:
            parts.append(60.0)
        elif bidders < 4:
            parts.append(72.0)
        elif bidders < 5:
            parts.append(85.0)
        else:
            parts.append(95.0)
    if watch_ratio is not None:
        if watch_ratio < 0.002:
            parts.append(40.0)
        elif watch_ratio < 0.003:
            parts.append(55.0)
        elif watch_ratio < 0.005:
            parts.append(70.0)
        elif watch_ratio < 0.008:
            parts.append(82.0)
        else:
            parts.append(90.0)
    return sum(parts) / len(parts) if parts else None


def _price_score(gap: float | None) -> float | None:
    if gap is None:
        return None
    return _clamp(100 - 100 * abs(gap) / PRICE_TOLERANCE)


def _delivery_score(delivery: dict) -> float:
    parts: list[float] = []
    rate = delivery["commission_rate"]
    if rate is not None:
        parts.append(100.0 if rate <= 0.03 else 85.0 if rate <= 0.05 else 65.0 if rate <= 0.08 else 45.0)
    fee = delivery["express_fee"]
    if fee is not None:
        parts.append(100.0 if fee <= 10 else 85.0 if fee <= 15 else 70.0 if fee <= 20 else 50.0)
    day = delivery["express_day"]
    if day is not None:
        parts.append(100.0 if day <= 2 else 85.0 if day <= 3 else 65.0 if day <= 5 else 45.0)
    parts.append(100.0 if delivery["is_storage"] else 60.0)
    return sum(parts) / len(parts)


def _sample_score(samples: int, min_samples: int) -> float | None:
    if samples < min_samples:
        return None
    if samples >= 30:
        return 100.0
    return 20.0 + 80.0 * (samples - min_samples) / (30 - min_samples)


def _total_score(scores: dict[str, float | None]) -> float | None:
    if scores["sample"] is None:
        return None
    available = {key: value for key, value in scores.items() if value is not None}
    weight = sum(WEIGHTS[key] for key in available)
    if not weight:
        return None
    return sum(WEIGHTS[key] * value for key, value in available.items()) / weight


# ---------------------------------------------------------------- 展示


def _risk_tags(
    *,
    relist: int,
    concentrated: bool,
    shill: bool,
    coverage: float,
    samples: int,
    gap_count: int,
    delivery: dict,
    min_samples: int,
) -> list[dict]:
    tags: list[dict] = []
    if relist >= RISK_RED:
        tags.append({"label": "同图重挂", "tone": "red"})
    elif relist:
        tags.append({"label": "同图重挂", "tone": "yellow"})
    if concentrated:
        tags.append({"label": "出价人集中", "tone": "red"})
    if shill:
        tags.append({"label": "疑似托价（参考）", "tone": "yellow"})
    if coverage < BID_COVERAGE_LOW:
        tags.append({"label": "热度覆盖不足", "tone": "yellow"})
    if samples < min_samples:
        tags.append({"label": "样本不足", "tone": "yellow"})
    elif gap_count < min_samples:
        tags.append({"label": "同标题可比不足", "tone": "yellow"})
    if _delivery_friendly(delivery):
        tags.append({"label": "交付友好", "tone": "green"})
    return tags


def _delivery_friendly(delivery: dict) -> bool:
    rate = delivery["commission_rate"]
    day = delivery["express_day"]
    return (
        rate is not None
        and rate <= 0.05
        and day is not None
        and day <= 3
        and delivery["is_storage"]
    )


def _recent_samples(sold) -> list[dict]:
    if not sold:
        return []
    latest = max(sold, key=lambda row: (row["auction_at"] or "", row["session_id"] or 0))
    same_session = [row for row in sold if row["session_id"] == latest["session_id"]]
    ranked = sorted(
        same_session,
        key=lambda row: (row["bid_user_num"] is not None, row["bid_user_num"] or 0),
        reverse=True,
    )[:5]
    return [
        {
            "title": row["title"],
            "price": _fmt_price(row["final_price"]),
            "meta": " · ".join(
                part
                for part in (
                    f"出价 {row['bid_user_num']} 人" if row["bid_user_num"] is not None else "",
                    f"{row['bid_num']} 次" if row["bid_num"] is not None else "",
                    (row["closed_at"] or "")[5:10],
                )
                if part
            ),
        }
        for row in ranked
    ]


def _session_bid_stats(group) -> tuple[float | None, float | None]:
    """专场级出价口径：人数按专场内出价人去重、次数按专场内拍品累加，再取场次中位数。

    出价人列表缺失（仅 `bid_user_num`）的拍品无法参与去重，按独立出价人计入；
    场次内所有拍品都没有对应数据时该场不进中位数。
    """
    sessions: dict = defaultdict(list)
    for row in group:
        sessions[row["session_id"]].append(row)

    bidders_values: list[int] = []
    bids_values: list[int] = []
    for rows in sessions.values():
        users: set = set()
        unknown = 0
        bids = None
        for row in rows:
            lot_users = set(_json_list(row["bid_users"]))
            if lot_users:
                users.update(lot_users)
            elif row["bid_user_num"]:
                unknown += row["bid_user_num"]
            if row["bid_num"] is not None:
                bids = (bids or 0) + row["bid_num"]
        if users or unknown:
            bidders_values.append(len(users) + unknown)
        if bids is not None:
            bids_values.append(bids)
    return (
        median(bidders_values) if bidders_values else None,
        median(bids_values) if bids_values else None,
    )


def _heat_detail(group, coverage: float, bidders: float | None, bids: float | None) -> dict:
    sessions: dict = defaultdict(list)
    for row in group:
        sessions[row["session_id"]].append(row)

    def session_median(key: str) -> float | None:
        values = []
        for rows in sessions.values():
            present = [row[key] for row in rows if row[key] is not None]
            if present:
                values.append(median(present))
        return median(values) if values else None

    viewers = session_median("viewer_count")
    views = session_median("view_count")
    return {
        "viewers": _fmt_count(None if viewers is None else round(viewers)),
        "views": _fmt_count(None if views is None else round(views)),
        "bidders": _fmt_bidders(bidders),
        "bids": _fmt_bidders(bids),
        "note": (
            f"场均按专场取中位数；出价人数按专场内出价人去重、次数按专场内拍品累加"
            f"（出价数据覆盖 {coverage:.0%}）。"
        ),
    }


def _verdict(tags: list[dict], samples: int, min_samples: int) -> str:
    if samples < min_samples:
        return "有效样本不足，暂不给结论。"
    labels = [tag["label"] for tag in tags]
    parts: list[str] = []
    if "同图重挂" not in labels:
        parts.append("未发现同图重挂")
    if "出价人集中" not in labels and "疑似托价（参考）" not in labels:
        parts.append("出价人分散")
    if "交付友好" in labels:
        parts.append("交付条件友好")
    if not parts:
        parts.append("存在风险标签，跟拍前建议逐项核对")
    return "，".join(parts) + "。"


def _radar_xy(value: float | None, index: int) -> tuple[float, float]:
    angle = math.radians(-90 + 72 * index)
    radius = 54 * (value or 0) / 100
    return round(120 + radius * math.cos(angle), 1), round(88 + radius * math.sin(angle), 1)


def _score_text(value: float | None) -> str:
    return "—" if value is None else str(round(value))


def _score_class(value: float | None) -> str:
    if value is None:
        return "score-none"
    if value >= 75:
        return "score-high"
    if value >= 55:
        return "score-mid"
    return "score-low"


def _fmt_pct(value: float | None, digits: int = 0) -> str:
    return "—" if value is None else f"{value:.{digits}%}"


def _fmt_gap(value: float | None) -> str:
    return "—" if value is None else f"{value:+.0%}"


def _fmt_bidders(value: float | None) -> str:
    return "—" if value is None else f"{round(value)}"


def _fmt_count(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _fmt_price(value: float | None) -> str:
    return "—" if value is None else f"¥{value:,.0f}"


def _delivery_text(delivery: dict) -> str:
    parts: list[str] = []
    if delivery["commission_rate"] is not None:
        parts.append(f"{delivery['commission_rate']:.0%}")
    if delivery["express_fee"] is not None:
        parts.append(f"¥{delivery['express_fee']:,.0f}")
    return " / ".join(parts) if parts else "—"


def _delivery_sub(delivery: dict) -> str:
    parts: list[str] = []
    if delivery["express_day"] is not None:
        parts.append(f"{delivery['express_day']} 天")
    parts.append("可寄存" if delivery["is_storage"] else "不可寄存")
    return " · ".join(parts)


def _is_sold(row) -> bool:
    return row["outcome"] == "sold" and row["final_price"] is not None


def _image_keys(row) -> list[str]:
    raw = row["image_keys"]
    return raw.split(",") if raw else []


def _json_list(raw) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []

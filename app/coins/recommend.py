"""推荐优先级：缺失年份 → 差品升级 → 跨系列相似（P4 年份 + P5 面值探索，spec §14.17）。

排序口径：
- P1 缺失年份：收藏位置权重 × 稀缺权重；位置权重首年/末年/执政者更替年 = 1.5，其余 1.0；
  稀缺权重 = 1 / max(rarity, floor)，rarity 取 Numista Frequency / 100。
- P2 差品升级：品相敏感度 × (1 / 稀有度)，敏感度 = XF 估值 / VF 估值（仅排序）；
  每个收藏系列最多取前 3 个，整体排在 P1 之后。
- P4 跨系列相似：仅当前两层不足时触发；同尺寸档位且差 ≤ 2mm、同面值层级、
  同执政者或历史时期重叠 ≥ 50%，满足其一即可；按尺寸差升序、稀有度降序。
- P5 面值探索：P4 命中不足时兜底，候选池同国家 + 面值即可，不卡年份；同一系列只取一条。

推荐生成后与「当日截拍专场」的已入库拍品做匹配；年份会翻译回站点写法
（日本：`明治6年` / `明治六年`）。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import date

from app import repository, textmatch
from app.coins import translate
from app.config import CoinCollectionConfig, Config
from app.models import (
    CoinCollectionProfile,
    CoinLot,
    CoinRecommendation,
    CoinTodayLot,
    CoinYearSpec,
)

_DIGITS = "零一二三四五六七八九"

# 日本年号（中文名 / 英文名 / 公历起止）；明治 1 年 = 1868
JAPANESE_ERAS: tuple[tuple[str, str, int, int], ...] = (
    ("明治", "meiji", 1868, 1912),
    ("大正", "taisho", 1912, 1926),
    ("昭和", "showa", 1926, 1989),
    ("平成", "heisei", 1989, 2019),
    ("令和", "reiwa", 2019, 2100),
)


def to_cn_numeral(number: int) -> str:
    """1~99 → 中文数字（明治六年 / 明治十五年）。"""
    if number < 0:
        return str(number)
    if number < 10:
        return _DIGITS[number]
    if number < 20:
        return "十" + (_DIGITS[number % 10] if number % 10 else "")
    if number < 100:
        return _DIGITS[number // 10] + "十" + (_DIGITS[number % 10] if number % 10 else "")
    return str(number)


def _is_japanese(profile: CoinCollectionProfile) -> bool:
    text = f"{profile.collection.country}{profile.collection.ruler}{profile.collection.name}"
    if "日本" in text or "japan" in text.lower():
        return True
    return any(zh in text or en in text.lower() for zh, en, _, _ in JAPANESE_ERAS)


def _japanese_era(profile: CoinCollectionProfile, spec: CoinYearSpec | None) -> str | None:
    """按执政者 / 系列名 / 公历年份定位年号；定位不到返回 None。"""
    text = f"{profile.collection.ruler or ''}{profile.collection.name}"
    for zh, en, _, _ in JAPANESE_ERAS:
        if zh in text or en in text.lower():
            return zh
    gregorian = spec.gregorian_year if spec else None
    if gregorian is not None:
        for zh, _, start, end in JAPANESE_ERAS:
            if start <= gregorian <= end:
                return zh
    return None


def year_labels(profile: CoinCollectionProfile, year: int, spec: CoinYearSpec | None) -> list[str]:
    """站点搜索 / 标题匹配用的年份写法（去重、保持顺序）。"""
    labels: list[str] = []
    if _is_japanese(profile):
        era = _japanese_era(profile, spec)
        if era:
            labels.append(f"{era}{year}年")
            labels.append(f"{era}{to_cn_numeral(year)}年")
            if year == 1:
                labels.append(f"{era}元年")

    labels.append(str(year))
    gregorian = spec.gregorian_year if spec else None
    if gregorian is not None and str(gregorian) not in labels:
        labels.append(str(gregorian))
    return list(dict.fromkeys(labels))


# 站点标题里的音译写法与 Numista 中文词表不一致：两边都生成，避免漏匹配
_DENOMINATION_ALIASES = {
    "莱普塔": ("雷普塔",),
    "雷普塔": ("莱普塔",),
}


def denomination_variants(denomination: str) -> list[str]:
    """面值写法：原样 / 去空格 / 阿拉伯数字转中文（2 钱 → 2钱 / 二钱）。"""
    text = (denomination or "").strip()
    if not text:
        return []
    bases = [text]
    for source, targets in _DENOMINATION_ALIASES.items():
        for base in list(bases):
            if source in base:
                bases.extend(base.replace(source, target) for target in targets)
    variants: list[str] = []
    for base in dict.fromkeys(bases):
        variants.extend([base, base.replace(" ", "")])
        match = re.match(r"^(\d+)\s*(.*)$", base)
        if match:
            number, rest = int(match.group(1)), match.group(2).strip()
            variants.append(f"{to_cn_numeral(number)}{rest}")
    return list(dict.fromkeys(variant for variant in variants if variant))


def _rarity(spec: CoinYearSpec | None) -> float | None:
    return spec.rarity if spec else None


def scarcity_weight(spec: CoinYearSpec | None, floor: float) -> float:
    """1 / max(rarity, floor)；没有 Frequency 时不给稀缺权重（按 1.0 中性处理）。"""
    rarity = _rarity(spec)
    if rarity is None:
        return 1.0
    return round(1 / max(rarity, floor), 6)


def _same_text(left: str | None, right: str | None) -> bool:
    return textmatch.normalize(translate.to_zh(left) or left) == textmatch.normalize(
        translate.to_zh(right) or right
    )


def _position_weight(profile: CoinCollectionProfile, year: int, profiles: list[CoinCollectionProfile]) -> float:
    """首年 / 末年 / 执政者更替年 = 1.5。"""
    if year in (profile.min_year, profile.max_year):
        return 1.5
    for other in profiles:
        if other.collection.collection_key == profile.collection.collection_key:
            continue
        if not _same_text(other.collection.country, profile.collection.country):
            continue
        if not _same_text(other.collection.denomination, profile.collection.denomination):
            continue
        if _same_text(other.collection.ruler, profile.collection.ruler):
            continue
        if year in (other.min_year, other.max_year):
            return 1.5
    return 1.0


def _sensitivity(spec: CoinYearSpec | None) -> float:
    """品相敏感度 = XF / VF；任一缺失或 VF 为 0 时按 1.0（不放大）。"""
    if spec is None or not spec.price_vf or spec.price_xf is None:
        return 1.0
    return round(spec.price_xf / spec.price_vf, 6)


def _spec(profile: CoinCollectionProfile, year: int) -> CoinYearSpec | None:
    specs = profile.year_specs.get(year) or []
    return specs[0] if specs else None


def _keyword(profile: CoinCollectionProfile, labels: list[str], denomination: str) -> str:
    parts = [profile.collection.country.strip(), labels[0] if labels else "", denomination.strip()]
    return " ".join(part for part in parts if part)


def _period_overlap(left: CoinCollectionProfile, right: CoinCollectionProfile) -> float:
    if left.min_year is None or left.max_year is None or right.min_year is None or right.max_year is None:
        return 0.0
    start = max(left.min_year, right.min_year)
    end = min(left.max_year, right.max_year)
    if end < start:
        return 0.0
    shorter = min(left.max_year - left.min_year + 1, right.max_year - right.min_year + 1)
    return (end - start + 1) / shorter if shorter else 0.0


def _profile_size(profile: CoinCollectionProfile) -> float | None:
    if profile.collection.size_mm is not None:
        return profile.collection.size_mm
    sizes = [
        spec.size_mm
        for specs in profile.year_specs.values()
        for spec in specs
        if spec.size_mm is not None
    ]
    return round(statistics.median(sizes), 2) if sizes else None


def _same_denomination_tier(
    left: str, right: str, tiers: tuple[tuple[str, ...], ...]
) -> bool:
    def norm(value: str) -> str:
        return textmatch.normalize(translate.to_zh(value) or value)

    left_key, right_key = norm(left), norm(right)
    for tier in tiers:
        normalized = {norm(value) for value in tier}
        if any(key in left_key or left_key in key for key in normalized) and any(
            key in right_key or right_key in key for key in normalized
        ):
            return True
    return False


def similar_profiles(
    profile: CoinCollectionProfile,
    profiles: list[CoinCollectionProfile],
    *,
    denomination_tiers: tuple[tuple[str, ...], ...] = (),
) -> list[tuple[CoinCollectionProfile, float]]:
    """返回相似收藏系列及尺寸差（越大越不相似；无法计算时用 999）。"""
    left_size = _profile_size(profile)
    result: list[tuple[CoinCollectionProfile, float]] = []
    for other in profiles:
        if other.collection.collection_key == profile.collection.collection_key:
            continue
        right_size = _profile_size(other)
        size_diff = abs(left_size - right_size) if left_size is not None and right_size is not None else None
        same_size = (
            size_diff is not None
            and size_diff <= 2
            and profile.size_band == other.size_band
        )
        same_tier = _same_denomination_tier(
            profile.collection.denomination, other.collection.denomination, denomination_tiers
        )
        same_ruler = bool(
            profile.collection.ruler
            and other.collection.ruler
            and profile.collection.ruler == other.collection.ruler
        )
        overlap = _period_overlap(profile, other) >= 0.5
        if same_size or same_tier or same_ruler or overlap:
            result.append((other, size_diff if size_diff is not None else 999.0))
    return result


def build_recommendations(
    profiles: list[CoinCollectionProfile],
    *,
    explore_profiles: list[CoinCollectionProfile] | None = None,
    search_results: int | None = None,
    similar_lots: list[CoinTodayLot] | None = None,
    exclude_lot_ids: set[int] | None = None,
    config: CoinCollectionConfig | None = None,
    rarity_floor: float = 0.005,
    min_results: int = 5,
    top_upgrades_per_series: int = 3,
    denomination_tiers: tuple[tuple[str, ...], ...] = (),
) -> list[CoinRecommendation]:
    """按优先级生成推荐（P1 → P2 → P4 → P5）。

    `search_results` 是前两层与已入库拍品的匹配条数；传了它就按「搜索结果」判断是否触发 P4，
    不传时退化为按前两层推荐项数判断（纯函数测试用）。

    `similar_lots` 给定时，P4 只保留**当日确有拍品命中**的候选；`exclude_lot_ids` 是精准匹配
    已命中的拍品，相似推荐不再使用。P4/P5 都是**每个候选系列一条**，同一拍品只归属一条推荐；
    不足 `min_results` 时用 P5「面值探索」补位（同国家 + 面值即算命中）。
    """
    if config is not None:
        rarity_floor = config.rarity_floor
        min_results = config.min_results
        top_upgrades_per_series = config.top_upgrades_per_series
        denomination_tiers = config.denomination_tiers

    items: list[CoinRecommendation] = []
    existing: set[tuple[str, int]] = set()

    # 优先级 1：缺失年份补齐
    missing_items: list[CoinRecommendation] = []
    for profile in profiles:
        for year in profile.missing_years:
            spec = _spec(profile, year)
            labels = year_labels(profile, year, spec)
            score = round(
                _position_weight(profile, year, profiles) * scarcity_weight(spec, rarity_floor), 6
            )
            missing_items.append(
                CoinRecommendation(
                    priority=1,
                    collection=profile.collection,
                    year=year,
                    year_labels=labels,
                    keyword=_keyword(profile, labels, profile.collection.denomination),
                    score=score,
                    reason="缺失年份",
                    spec=spec,
                )
            )
    missing_items.sort(key=lambda item: (-item.score, item.collection.name, item.year))
    for item in missing_items:
        items.append(item)
        existing.add((item.collection.collection_key, item.year))

    # 优先级 2：差品升级（每个系列最多 top 个）
    upgrade_items: list[CoinRecommendation] = []
    for profile in profiles:
        per_series: list[CoinRecommendation] = []
        for year in profile.upgrade_years:
            spec = _spec(profile, year)
            labels = year_labels(profile, year, spec)
            score = round(_sensitivity(spec) * scarcity_weight(spec, rarity_floor), 6)
            per_series.append(
                CoinRecommendation(
                    priority=2,
                    collection=profile.collection,
                    year=year,
                    year_labels=labels,
                    keyword=_keyword(profile, labels, profile.collection.denomination),
                    score=score,
                    reason="差品升级",
                    spec=spec,
                )
            )
        per_series.sort(key=lambda item: (-item.score, item.year))
        upgrade_items.extend(per_series[:top_upgrades_per_series])
    upgrade_items.sort(key=lambda item: (-item.score, item.collection.name, item.year))
    for item in upgrade_items:
        key = (item.collection.collection_key, item.year)
        if key in existing:
            continue
        items.append(item)
        existing.add(key)

    # 优先级 4：跨系列相似（候选池国家 + 年份 + 面值；前两层搜索结果不足时兜底）
    enough = search_results >= min_results if search_results is not None else len(items) >= min_results
    if enough:
        return items

    explore = explore_profiles if explore_profiles is not None else profiles
    candidates: list[tuple[float, float, CoinRecommendation]] = []
    seen: set[tuple[str, int]] = set()
    for profile in profiles:
        for other, size_diff in similar_profiles(
            profile, explore, denomination_tiers=denomination_tiers
        ):
            for year in other.missing_years:
                key = (other.collection.collection_key, year)
                if key in existing or key in seen:
                    continue
                spec = _spec(other, year)
                labels = year_labels(other, year, spec)
                candidates.append(
                    (
                        size_diff,
                        -(spec.rarity if spec and spec.rarity is not None else 999),
                        CoinRecommendation(
                            priority=4,
                            collection=other.collection,
                            year=year,
                            year_labels=labels,
                            keyword=_keyword(other, labels, other.collection.denomination),
                            score=round(1 / (size_diff + 1), 6),
                            reason="年份匹配",
                            spec=spec,
                        ),
                    )
                )
                seen.add(key)

    candidates.sort(key=lambda entry: (entry[0], entry[1], entry[2].collection.name, entry[2].year))

    # 优先级 5：面值探索（候选池国家 + 面值，不卡年份）；系列去重在选取阶段处理
    explore_candidates: list[tuple[float, float, CoinRecommendation]] = []
    for size_diff, rarity_key, item in candidates:
        explore_candidates.append(
            (
                size_diff,
                rarity_key,
                CoinRecommendation(
                    priority=5,
                    collection=item.collection,
                    year=item.year,
                    year_labels=item.year_labels,
                    keyword=" ".join(
                        part
                        for part in [
                            item.collection.country.strip(),
                            item.collection.denomination.strip(),
                        ]
                        if part
                    ),
                    score=item.score,
                    reason="面值探索",
                    spec=item.spec,
                ),
            )
        )

    if similar_lots is not None:
        # 先匹配再取数：排除精准命中的拍品；第一轮每卡 1 条，第二轮按剩余名额多条
        pool = [lot for lot in similar_lots if lot.id not in (exclude_lot_ids or set())]
        added_lots = 0
        used_series: set[str] = set()
        used_lots: set[int] = set()
        used_keys: set[tuple[str, int]] = set()
        series_cards: dict[str, CoinRecommendation] = {}

        def add(item: CoinRecommendation, *, new_card: bool) -> bool:
            nonlocal added_lots
            if added_lots >= min_results:
                return False
            series_key = item.collection.collection_key
            key = (series_key, item.year)
            if key in used_keys:
                return False
            if new_card and series_key in used_series:
                return False
            if not new_card and series_key not in series_cards:
                return False
            matched = [
                lot for lot in pool if lot.id not in used_lots and lot_matches(item, lot)
            ]
            if not matched:
                return False
            quota = 1 if new_card else min(len(matched), min_results - added_lots)
            if new_card:
                item.lot_limit = quota
                items.append(item)
                series_cards[series_key] = item
            else:
                series_cards[series_key].lot_limit += quota
            used_keys.add(key)
            used_series.add(series_key)
            used_lots.update(lot.id for lot in matched[:quota] if lot.id is not None)
            added_lots += quota
            return True

        # 第一轮：P4 每个系列一条，再用 P5 每个系列一条，各挂 1 条拍品
        for _, _, item in candidates:
            add(item, new_card=True)
        for _, _, item in explore_candidates:
            add(item, new_card=True)
        # 不同系列不足 min_results：同一系列继续挂多条拍品，不新开卡
        if len(used_series) < min_results:
            for _, _, item in candidates:
                add(item, new_card=False)
            for _, _, item in explore_candidates:
                add(item, new_card=False)
        return items

    # 纯函数模式（没有拍品可匹配）：P4 先补位，不足再用 P5 补；每个系列一条
    base_count = search_results if search_results is not None else len(items)
    added = 0
    used_series = set()

    def add(item: CoinRecommendation) -> bool:
        nonlocal added
        if base_count + added >= min_results:
            return False
        if item.collection.collection_key in used_series:
            return False
        items.append(item)
        used_series.add(item.collection.collection_key)
        added += 1
        return True

    for _, _, item in candidates:
        add(item)
    for _, _, item in explore_candidates:
        add(item)
    return items


# ---------------------------------------------------------------- 与已入库拍品匹配


@dataclass(frozen=True)
class LotMatch:
    lot_id: int
    score: float
    keyword: str
    recommendation: CoinRecommendation


def lot_matches(item: CoinRecommendation, lot: CoinLot | CoinTodayLot) -> bool:
    """拍品标题必须同时命中国家、面值；P5 面值探索不卡年份。"""
    title = lot.title
    if not textmatch.contains(title, item.collection.country):
        return False
    if not any(
        textmatch.contains(title, variant)
        for variant in denomination_variants(item.collection.denomination)
    ):
        return False
    if item.priority == 5:
        return True
    return any(textmatch.contains(title, label) for label in item.year_labels)


def match_lots(
    items: list[CoinRecommendation], lots: list[CoinLot] | list[CoinTodayLot]
) -> dict[int, LotMatch]:
    """一条拍品只记最优匹配（优先级高者优先，其次分数高者）。"""
    best: dict[int, LotMatch] = {}
    for lot in lots:
        if lot.id is None:
            continue
        for item in items:
            if not lot_matches(item, lot):
                continue
            current = best.get(lot.id)
            if current is None or (item.priority, -item.score) < (
                current.recommendation.priority,
                -current.recommendation.score,
            ):
                best[lot.id] = LotMatch(lot.id, item.score, item.keyword, item)
    return best


def attach_lots(
    items: list[CoinRecommendation], lots: list[CoinTodayLot]
) -> list[CoinRecommendation]:
    """把匹配到的当日拍品挂到推荐项上（页面展示用）。"""
    for item in items:
        item.matched_lots = [lot for lot in lots if lot_matches(item, lot)]
    return items


def _prune_similar_lots(
    items: list[CoinRecommendation], exclude_lot_ids: set[int]
) -> list[CoinRecommendation]:
    """相似推荐不展示精准命中的拍品；每卡按 `lot_limit` 取拍品，且同一条拍品只归属一条推荐。"""
    used: set[int] = set()
    for item in items:
        if item.priority not in (4, 5):
            continue
        kept = []
        for lot in item.matched_lots:
            if lot.id is None or lot.id in exclude_lot_ids or lot.id in used:
                continue
            kept.append(lot)
            used.add(lot.id)
            if len(kept) >= item.lot_limit:
                break
        item.matched_lots = kept
    return items


@dataclass(frozen=True)
class MatchRunResult:
    lots_total: int
    lots_recommended: int
    items_total: int
    keywords: dict[int, str]
    profiles_total: int = 0


def rematch_all(
    config: Config,
    *,
    profiles: list[CoinCollectionProfile] | None = None,
    items: list[CoinRecommendation] | None = None,
) -> MatchRunResult:
    """按推荐优先级重算拍品推荐位；只有**当日截拍专场**的拍品会被推荐。

    画像为空（还没配收藏系列映射）时返回空结果，由调用方回退旧的标签匹配。
    """
    from app.coins import collection as collection_module

    active_profiles = profiles if profiles is not None else collection_module.load_profiles()
    if not active_profiles:
        return MatchRunResult(0, 0, 0, {})
    today_lots = repository.list_coin_today_lots(config.app.tz)
    if items is not None:
        active_items = items
    else:
        base_items = build_recommendations(
            active_profiles, explore_profiles=[], config=config.coins.collection
        )
        base_matches = match_lots(base_items, today_lots)
        active_items = build_recommendations(
            active_profiles,
            explore_profiles=collection_module.load_explore_profiles(),
            search_results=len(base_matches),
            similar_lots=today_lots,
            exclude_lot_ids=set(base_matches),
            config=config.coins.collection,
        )
    matches = match_lots(active_items, today_lots)

    lots = repository.list_coin_lots_for_matching()
    recommended = 0
    keywords: dict[int, str] = {}
    for lot in lots:
        if lot.id is None:
            continue
        match = matches.get(lot.id)
        repository.update_coin_lot_match(lot.id, match.score if match else 0.0, bool(match))
        if match is not None:
            recommended += 1
            keywords[lot.id] = match.keyword
    return MatchRunResult(
        len(lots), recommended, len(active_items), keywords, profiles_total=len(active_profiles)
    )


def rematch_today(config: Config) -> MatchRunResult:
    """只重算当日截拍拍品的推荐位（页面打开前刷新，不必等每日采集任务）。"""
    from app.coins import collection as collection_module

    active_profiles = collection_module.load_profiles()
    if not active_profiles:
        return MatchRunResult(0, 0, 0, {})
    today_lots = repository.list_coin_today_lots(config.app.tz)
    base_items = build_recommendations(
        active_profiles, explore_profiles=[], config=config.coins.collection
    )
    base_matches = match_lots(base_items, today_lots)
    active_items = build_recommendations(
        active_profiles,
        explore_profiles=collection_module.load_explore_profiles(),
        search_results=len(base_matches),
        similar_lots=today_lots,
        config=config.coins.collection,
    )
    matches = match_lots(active_items, today_lots)

    updates: list[tuple[int, float, bool]] = []
    keywords: dict[int, str] = {}
    for lot in today_lots:
        if lot.id is None:
            continue
        match = matches.get(lot.id)
        updates.append((lot.id, match.score if match else 0.0, match is not None))
        if match is not None:
            keywords[lot.id] = match.keyword
    repository.update_coin_lot_matches(updates)
    return MatchRunResult(
        len(today_lots),
        len(matches),
        len(active_items),
        keywords,
        profiles_total=len(active_profiles),
    )


def load_recommendations(
    config: Config, *, day: date | None = None
) -> tuple[list[CoinCollectionProfile], list[CoinRecommendation]]:
    """加载画像与推荐项；`day` 给定时匹配该业务日期截拍的拍品（默认今天）。"""
    from app.coins import collection as collection_module

    profiles = collection_module.load_profiles()
    if not profiles:
        return [], []
    lots = (
        repository.list_coin_lots_on_day(config.app.tz, day)
        if day is not None
        else repository.list_coin_today_lots(config.app.tz)
    )
    base_items = build_recommendations(
        profiles, explore_profiles=[], config=config.coins.collection
    )
    base_matches = match_lots(base_items, lots)
    items = build_recommendations(
        profiles,
        explore_profiles=collection_module.load_explore_profiles(),
        search_results=len(base_matches),
        similar_lots=lots,
        exclude_lot_ids=set(base_matches),
        config=config.coins.collection,
    )
    return profiles, _prune_similar_lots(attach_lots(items, lots), set(base_matches))

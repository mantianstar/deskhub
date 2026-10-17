"""收藏画像：Numista 系列 + 历史订单品相 → 收藏系列画像（spec §14.17）。

数据链路：
1. `coin_collection_series[_members]` 手工把多个 Numista series 合并成一个收藏系列；
2. 只保留材质为 Bronze / Copper 的系列，并剔除精制币（Proof）；
3. 按年份聚合单版：同一年多个普制版别时取**发行量最大**的一条；
4. 历史订单（`coin_orders`）与人工品相（`coin_purchase_grades`）按
   「国家 + 年份 + 面值」回填「已有年份」；品相为「差」的年份进入需升级列表。

职责边界：本模块不依赖 FastAPI；SQL 一律走 `app.repository`。
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict

from app import repository, textmatch
from app.coins import grades as coin_grades
from app.coins import translate
from app.models import (
    CoinCollectionMember,
    CoinCollectionProfile,
    CoinCollectionSeries,
    CoinIssue,
    CoinIssueValues,
    CoinOrder,
    CoinPurchaseGrade,
    CoinSeries,
    CoinYearSpec,
)

# 尺寸档位（spec §14.17 第 2 条）；<25mm 归入「小」，让所有铜币都有档位
SIZE_BAND_SMALL = "小"
SIZE_BAND_MEDIUM = "中"
SIZE_BAND_LARGE = "大"
SIZE_BAND_XL = "超大"
SIZE_BANDS = (SIZE_BAND_SMALL, SIZE_BAND_MEDIUM, SIZE_BAND_LARGE, SIZE_BAND_XL)

_PROOF_PATTERN = re.compile(r"\bproof\b|精制", re.IGNORECASE)


def size_band(size_mm: float | None) -> str | None:
    """尺寸分档：中 [25, 30) / 大 [30, 35) / 超大 [35, ∞)；<25 记为「小」。"""
    if size_mm is None:
        return None
    if size_mm < 25:
        return SIZE_BAND_SMALL
    if size_mm < 30:
        return SIZE_BAND_MEDIUM
    if size_mm < 35:
        return SIZE_BAND_LARGE
    return SIZE_BAND_XL


def is_bronze_or_copper(composition: str | None) -> bool:
    """材质限定 Bronze / Copper；铜镍合金（Copper-nickel）不算。"""
    text = (composition or "").strip().lower()
    if not text:
        return False
    if "bronze" in text or "青铜" in text:
        return True
    if "copper" in text or text == "铜":
        return "nickel" not in text and "silver" not in text and "镍" not in text and "银" not in text
    return False


def is_proof_issue(issue: CoinIssue, series: CoinSeries | None = None) -> bool:
    """精制币剔除：看 `is_proof`，再看**该单版**备注 / 版别里是否出现 Proof / 精制。

    系列级描述不参与判断 —— 一个系列里有精制版，不代表所有年份都是精制。
    """
    if issue.is_proof:
        return True
    return any(
        text and _PROOF_PATTERN.search(text) for text in (issue.comment, issue.variety)
    )


def _norm_country(value: str | None) -> str:
    return coin_grades.normalize_country(translate.to_zh(value) or value)


def _norm_denomination(value: str | None) -> str:
    return coin_grades.normalize_denomination(translate.to_zh(value) or value)


def _same_identity(value: str | None, target: str) -> bool:
    """宽松包含（同 `app.textmatch`），任一方向命中都算。"""
    if not value or not target:
        return False
    return textmatch.contains(value, target) or textmatch.contains(target, value)


def _parse_year(value: str | None) -> int | None:
    text = coin_grades.normalize_year(value)
    return int(text) if text.isdigit() else None


def _rarity(values: CoinIssueValues | None) -> float | None:
    """Numista Frequency（百分数）→ 0~1 的稀有度；缺失时返回 None。"""
    if values is None or values.frequency is None:
        return None
    return round(values.frequency / 100, 6)


def _values_by_issue(values: list[CoinIssueValues]) -> dict[int, CoinIssueValues]:
    return {item.numista_id: item for item in values}


def build_year_specs(
    issues: list[CoinIssue],
    values: list[CoinIssueValues],
    *,
    series: CoinSeries | None = None,
) -> dict[int, list[CoinYearSpec]]:
    """按年份聚合单版；剔除精制币，同一年只留发行量最大的一条。"""
    by_issue = _values_by_issue(values)
    grouped: dict[int, list[CoinYearSpec]] = defaultdict(list)
    for issue in issues:
        if issue.year is None or is_proof_issue(issue, series):
            continue
        value = by_issue.get(issue.numista_id)
        grouped[int(issue.year)].append(
            CoinYearSpec(
                year=int(issue.year),
                gregorian_year=issue.gregorian_year,
                numista_issue_id=issue.numista_id,
                mintage=issue.mintage,
                rarity=_rarity(value),
                size_mm=issue.size_mm,
                weight_g=issue.weight_g,
                price_vf=value.value_vf if value else None,
                price_xf=value.value_xf if value else None,
                currency=value.currency if value else None,
                is_proof=issue.is_proof,
            )
        )

    result: dict[int, list[CoinYearSpec]] = {}
    for year, specs in grouped.items():
        # 有发行量的优先；发行量相同或都缺失时按单版号稳定排序
        specs.sort(key=lambda spec: (spec.mintage is not None, spec.mintage or 0, spec.numista_issue_id), reverse=True)
        result[year] = specs[:1]
    return dict(sorted(result.items()))


def _collection_size(collection: CoinCollectionSeries, series_list: list[CoinSeries]) -> float | None:
    if collection.size_mm is not None:
        return collection.size_mm
    sizes = [series.size_mm for series in series_list if series.size_mm is not None]
    return round(statistics.median(sizes), 2) if sizes else None


def _owned_years(
    collection: CoinCollectionSeries,
    years: list[int],
    orders: list[CoinOrder],
    grades: list[CoinPurchaseGrade],
) -> tuple[list[int], list[int]]:
    """回填已有年份与需升级年份（品相为「差」）。

    匹配口径是「国家 + 年份 + 面值」：国家 / 面值先按 Numista 口径统一，再做宽松包含。
    """
    year_set = set(years)
    country = _norm_country(collection.country)
    denomination = _norm_denomination(collection.denomination)
    owned: set[int] = set()
    bad_grades: set[int] = set()

    for order in orders:
        if not (_same_identity(_norm_country(order.country), country) and _same_identity(_norm_denomination(order.denomination), denomination)):
            continue
        year = _parse_year(order.era)
        if year in year_set:
            owned.add(year)

    for grade in grades:
        if not (_same_identity(_norm_country(grade.country), country) and _same_identity(_norm_denomination(grade.denomination), denomination)):
            continue
        year = _parse_year(grade.era)
        if year not in year_set:
            continue
        owned.add(year)
        if grade.grade == "差":
            bad_grades.add(year)

    return sorted(owned), sorted(bad_grades)


def build_profile(
    collection: CoinCollectionSeries,
    member_series: list[CoinSeries],
    *,
    issues_by_series: dict[int, list[CoinIssue]],
    values_by_series: dict[int, list[CoinIssueValues]],
    orders: list[CoinOrder],
    grades: list[CoinPurchaseGrade],
) -> CoinCollectionProfile:
    """组装一个收藏系列的画像。"""
    eligible = [series for series in member_series if is_bronze_or_copper(series.composition)]
    specs_by_year: dict[int, list[CoinYearSpec]] = {}
    for series in eligible:
        series_id = series.numista_id
        specs = build_year_specs(
            issues_by_series.get(series_id, []),
            values_by_series.get(series_id, []),
            series=series,
        )
        for year, year_specs in specs.items():
            current = specs_by_year.get(year)
            if current is None or (year_specs[0].mintage or 0) > (current[0].mintage or 0):
                specs_by_year[year] = year_specs

    specs_by_year = dict(sorted(specs_by_year.items()))
    years = sorted(specs_by_year)
    owned, upgrade = _owned_years(collection, years, orders, grades)
    missing = [year for year in years if year not in set(owned)]
    size_mm = _collection_size(collection, eligible)
    return CoinCollectionProfile(
        collection=collection,
        member_series_ids=[series.numista_id for series in member_series],
        min_year=years[0] if years else None,
        max_year=years[-1] if years else None,
        years=years,
        owned_years=owned,
        missing_years=missing,
        upgrade_years=upgrade,
        year_specs=specs_by_year,
        size_band=size_band(size_mm),
    )


def build_profiles(
    collections: list[CoinCollectionSeries],
    members: list[CoinCollectionMember],
    series_by_numista_id: dict[int, CoinSeries],
    *,
    issues_by_series: dict[int, list[CoinIssue]],
    values_by_series: dict[int, list[CoinIssueValues]],
    orders: list[CoinOrder],
    grades: list[CoinPurchaseGrade],
) -> list[CoinCollectionProfile]:
    """批量组装画像；没有成员映射的收藏系列也会返回（空画像）。"""
    members_by_collection: dict[int, list[CoinCollectionMember]] = defaultdict(list)
    for member in members:
        members_by_collection[member.collection_id].append(member)

    profiles: list[CoinCollectionProfile] = []
    for collection in collections:
        member_rows = members_by_collection.get(collection.id or 0, [])
        member_series = [
            series_by_numista_id[member.numista_series_id]
            for member in member_rows
            if member.numista_series_id in series_by_numista_id
        ]
        profiles.append(
            build_profile(
                collection,
                member_series,
                issues_by_series=issues_by_series,
                values_by_series=values_by_series,
                orders=orders,
                grades=grades,
            )
        )
    return profiles


def _load_issue_data(
    series_list: list[CoinSeries],
) -> tuple[dict[int, list[CoinIssue]], dict[int, list[CoinIssueValues]]]:
    issues_by_series: dict[int, list[CoinIssue]] = {}
    values_by_series: dict[int, list[CoinIssueValues]] = {}
    for series in series_list:
        if series.id is None:
            continue
        issues_by_series[series.numista_id] = repository.list_coin_issues(series.id, cn=True)
        values_by_series[series.numista_id] = repository.list_coin_issue_values(series.numista_id)
    return issues_by_series, values_by_series


def load_profiles() -> list[CoinCollectionProfile]:
    """从库里读全量数据并组装画像（页面 / 推荐 / 管道共用）。"""
    collections = repository.list_coin_collection_series()
    if not collections:
        return []
    members = repository.list_coin_collection_members()
    member_ids = {member.numista_series_id for member in members}
    series_cn = {
        series.numista_id: series
        for series in repository.list_coin_series(cn=True)
        if series.numista_id in member_ids
    }
    issues_by_series, values_by_series = _load_issue_data(list(series_cn.values()))
    return build_profiles(
        collections,
        members,
        series_cn,
        issues_by_series=issues_by_series,
        values_by_series=values_by_series,
        orders=repository.list_coin_orders(),
        grades=repository.list_coin_purchase_grades(),
    )


def _cluster_by_size(series_list: list[CoinSeries], max_diff: float = 2.0) -> list[list[CoinSeries]]:
    """按尺寸差 ≤ max_diff 聚成一组（无尺寸的单独成组）。"""
    sized = sorted(
        (series for series in series_list if series.size_mm is not None),
        key=lambda series: (series.size_mm, series.numista_id),
    )
    unsized = [series for series in series_list if series.size_mm is None]
    clusters: list[list[CoinSeries]] = []
    for series in sized:
        if clusters and series.size_mm - clusters[-1][-1].size_mm <= max_diff:
            clusters[-1].append(series)
        else:
            clusters.append([series])
    clusters.extend([series] for series in unsized)
    return clusters


def _explore_collection(cluster: list[CoinSeries]) -> CoinCollectionSeries:
    ids = sorted(series.numista_id for series in cluster)
    first = cluster[0]
    sizes = [series.size_mm for series in cluster if series.size_mm is not None]
    size_mm = round(statistics.median(sizes), 2) if sizes else None
    now = repository.utcnow()
    return CoinCollectionSeries(
        id=None,
        collection_key=f"explore:{ids[0]}-{ids[-1]}",
        name=" · ".join(
            part for part in (first.issuer, first.ruler or "-", first.denomination) if part
        ),
        country=first.issuer or "",
        ruler=first.ruler,
        denomination=first.denomination or "",
        size_mm=size_mm,
        size_band=size_band(size_mm),
        created_at=now,
        updated_at=now,
    )


def load_explore_profiles() -> list[CoinCollectionProfile]:
    """未被手工映射的青铜/铜系列：按「同国家 + 同执政者 + 同面值 + 尺寸差 ≤ 2mm」自动成组。

    只用于第三层「跨系列相似推荐」——这些系列不在收藏画像里，但可以作为探索目标。
    """
    mapped = {member.numista_series_id for member in repository.list_coin_collection_members()}
    series_cn = [series for series in repository.list_coin_series(cn=True)]
    candidates = [
        series
        for series in series_cn
        if series.numista_id not in mapped and is_bronze_or_copper(series.composition)
    ]
    if not candidates:
        return []

    grouped: dict[tuple[str, str, str], list[CoinSeries]] = defaultdict(list)
    for series in candidates:
        grouped[
            (series.issuer or "", series.ruler or "", series.denomination or "")
        ].append(series)

    issues_by_series, values_by_series = _load_issue_data(candidates)
    orders = repository.list_coin_orders()
    grades = repository.list_coin_purchase_grades()
    profiles: list[CoinCollectionProfile] = []
    for group in grouped.values():
        for cluster in _cluster_by_size(group):
            profile = build_profile(
                _explore_collection(cluster),
                cluster,
                issues_by_series=issues_by_series,
                values_by_series=values_by_series,
                orders=orders,
                grades=grades,
            )
            if profile.years:
                profiles.append(profile)
    return profiles

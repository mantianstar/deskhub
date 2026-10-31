"""收藏画像：Numista 系列 + 历史订单品相 → 收藏系列画像（spec §14.17）。

数据链路：
1. `coin_collection_series[_members]` 手工把多个 Numista series 合并成一个收藏系列；
2. 只保留材质为 Bronze / Copper 的系列，并剔除精制币（Proof）；
3. 按年份聚合单版：同一年多个普制版别时取**发行量最大**的一条；
4. 人工品相（`coin_purchase_grades`）按「国家 + 年份 + 面值」回填「已有年份」：
   品相按 好 / 普通 / 差 三个值匹配，为「差」的年份另外进入需升级列表；
   未录入品相（有订单但实物缺失）不视为已有，计入缺失年份。

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
_COMMEMORATIVE_PATTERN = re.compile(r"纪念|commemorative", re.IGNORECASE)


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
    """材质限定 Bronze / Copper；铜镍 / 黄铜 / 白铜不算。

    中文表会写成「铜 (98% 铜, 1% 锡, 1% 锌)」，不能只认单独的「铜」。
    """
    text = (composition or "").strip().lower()
    if not text:
        return False
    if "bronze" in text or "青铜" in text:
        return True
    if "copper" in text or "铜" in text:
        excluded = ("nickel", "silver", "镍", "银", "黄铜", "白铜")
        return not any(word in text for word in excluded)
    return False


def is_commemorative_object(object_type: str | None) -> bool:
    """纪念币：Numista `object_type` 含「纪念 / commemorative」——材质不限。

    中国流通纪念币多为镍包钢 / 黄铜 / 双色铜合金，单看材质会被 `is_bronze_or_copper`
    全部剔掉（spec §14.17）；纪念币走这条旁路进入画像。
    """
    return bool(object_type and _COMMEMORATIVE_PATTERN.search(object_type))


def is_eligible_series(series: CoinSeries) -> bool:
    """可进画像 / 可选为成员的系列：青铜 / 铜，或纪念币（不限材质）。"""
    return is_bronze_or_copper(series.composition) or is_commemorative_object(
        series.object_type
    )


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


def _norm_denomination(value: str | None, country: str | None = None) -> str:
    """面值：先按 Numista 口径统一；香港的「分」与「仙」同义（英文 Cent 译作「分」）。"""
    text = coin_grades.normalize_denomination(translate.to_zh(value) or value)
    if _norm_country(country) == "香港" and text.endswith("分"):
        return f"{text[:-1]}仙"
    return text


def _same_identity(value: str | None, target: str) -> bool:
    """宽松包含（同 `app.textmatch`），任一方向命中都算。"""
    if not value or not target:
        return False
    return textmatch.contains(value, target) or textmatch.contains(target, value)


def _parse_year(value: str | None) -> int | None:
    text = coin_grades.year_number(value)
    return int(text) if text.isdigit() else None


def _parse_gregorian(value: str | None) -> int | None:
    """年份里的公历年（日本年号已换算；`1910年` / `明治十五年` → 1910 / 1882）。"""
    text = coin_grades.normalize_year(value)
    return int(text) if len(text) == 4 and text.isdigit() else None


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
                comment=issue.comment,
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
    denomination: str | None,
    theme: str,
    coin_name: str,
    specs_by_year: dict[tuple[int, str], CoinYearSpec],
    grades: list[CoinPurchaseGrade],
) -> tuple[list[int], list[int]]:
    """回填已有年份与需升级年份（品相为「差」）。

    已有年份只看人工品相（好 / 普通 / 差）；未录入品相的组合即使有订单也按实物缺失处理，
    不计入已有年份，从而落进缺失年份。
    匹配口径是「国家 + 年份 + 面值」，面值 / 发行系列 / 发行主题取**收藏系列成员**的值：
    同一收藏系列下的多个成员（如 女排 / 男排）各匹配各的品相行，不互相串。
    国家 / 面值先按 Numista 口径统一，再做宽松包含。
    系列里有公历年时按公历年关联（日本年号：明治 6 年 = 1873、大正 6 年 = 1917）；
    品相记录本身没有公历年（如裸数字 `6`）才退回年号数字。
    """
    year_set = {year for year, _ in specs_by_year}
    gregorian_years = {
        spec.gregorian_year: year
        for (year, _), spec in specs_by_year.items()
        if spec.gregorian_year is not None
    }
    country = _norm_country(collection.country)
    target_denomination = _norm_denomination(denomination, country)
    # 品相行的 theme / coin_name 分别对应「发行系列 / 发行主题」；
    # 双方都有值时才比较，一侧留空视为不限（普通系列与旧数据不受影响）
    issue_series = (theme or "").strip()
    issue_theme = (coin_name or "").strip()
    owned: set[int] = set()
    bad_grades: set[int] = set()

    def matched_year(value: str | None) -> int | None:
        gregorian = _parse_gregorian(value)
        if gregorian is not None and gregorian_years:
            matched = gregorian_years.get(gregorian)
            if matched is not None:
                return matched
            # 公历年表可能只覆盖部分单版（如南非 1927 没录 gregorian_year）；
            # 年份本身就是公历年时，直接按年份键匹配
            return gregorian if gregorian in year_set else None
        year = _parse_year(value)
        return year if year in year_set else None

    for grade in grades:
        if not (_same_identity(_norm_country(grade.country), country) and _same_identity(_norm_denomination(grade.denomination, country), target_denomination)):
            continue
        grade_issue_series = (grade.theme or "").strip()
        if issue_series and grade_issue_series and grade_issue_series != issue_series:
            continue
        grade_issue_theme = (grade.coin_name or "").strip()
        if issue_theme and grade_issue_theme and grade_issue_theme != issue_theme:
            continue
        year = matched_year(grade.era)
        if year is None:
            continue
        owned.add(year)
        if grade.grade == "差":
            bad_grades.add(year)

    return sorted(owned), sorted(bad_grades)


def build_profile(
    collection: CoinCollectionSeries,
    member_series: list[CoinSeries],
    *,
    member: CoinCollectionMember | None = None,
    issues_by_series: dict[int, list[CoinIssue]],
    values_by_series: dict[int, list[CoinIssueValues]],
    grades: list[CoinPurchaseGrade],
) -> CoinCollectionProfile:
    """组装一个收藏系列**成员**的画像（无成员时是空画像）。"""
    eligible = [
        series for series in member_series if is_eligible_series(series)
    ]
    # (年份, 发行主题) 唯一：纪念币按发行主题区分同年的不同主题
    name = next(
        (
            (series.issue_theme or "").strip()
            for series in eligible
            if is_commemorative_object(series.object_type)
            and (series.issue_theme or "").strip()
        ),
        "",
    )
    issue_series = next(
        (
            (series.issue_series or "").strip()
            for series in eligible
            if (series.issue_series or "").strip()
        ),
        "",
    )
    specs_by_year: dict[tuple[int, str], CoinYearSpec] = {}
    for series in eligible:
        series_id = series.numista_id
        specs = build_year_specs(
            issues_by_series.get(series_id, []),
            values_by_series.get(series_id, []),
            series=series,
        )
        for year, year_specs in specs.items():
            key = (year, name)
            current = specs_by_year.get(key)
            if current is None or (year_specs[0].mintage or 0) > (current.mintage or 0):
                specs_by_year[key] = year_specs[0]

    specs_by_year = dict(sorted(specs_by_year.items()))
    years = sorted({year for year, _ in specs_by_year})
    # 品相按**成员面值**匹配：同一收藏系列下可能有多个面值的成员；
    # 成员没有面值时退回收藏系列的面值
    member_denomination = next(
        (
            series.denomination
            for series in member_series
            if (series.denomination or "").strip()
        ),
        collection.denomination,
    )
    owned, upgrade = _owned_years(
        collection,
        member_denomination,
        issue_series,
        name,
        specs_by_year,
        grades,
    )
    missing = [year for year in years if year not in set(owned)]
    size_mm = _collection_size(collection, eligible)
    commemorated_topic = next(
        (
            (series.commemorated_topic or "").strip()
            for series in eligible
            if (series.commemorated_topic or "").strip()
        ),
        "",
    )
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
        size_mm=size_mm,
        size_band=size_band(size_mm),
        member=member,
        year_names={year: name for year in years} if name else {},
        commemorated_topic=commemorated_topic,
        issue_series=issue_series,
        issue_theme=name,
        is_commemorative=any(
            is_commemorative_object(series.object_type) for series in eligible
        ),
        owned_keys={(year, name) for year in owned},
        upgrade_keys={(year, name) for year in upgrade},
    )


def build_member_profiles(
    collections: list[CoinCollectionSeries],
    members: list[CoinCollectionMember],
    series_by_numista_id: dict[int, CoinSeries],
    *,
    issues_by_series: dict[int, list[CoinIssue]],
    values_by_series: dict[int, list[CoinIssueValues]],
    grades: list[CoinPurchaseGrade],
) -> list[CoinCollectionProfile]:
    """批量组装**成员级**画像：每个成员一张卡；没有成员映射的收藏系列返回空画像。

    内部实现：页面的收藏系列编辑区与收藏系列级画像共用，对外用 `build_profiles`。
    """
    members_by_collection: dict[int, list[CoinCollectionMember]] = defaultdict(list)
    for member in members:
        members_by_collection[member.collection_id].append(member)

    profiles: list[CoinCollectionProfile] = []
    for collection in collections:
        member_rows = members_by_collection.get(collection.id or 0, [])
        if not member_rows:
            profiles.append(
                build_profile(
                    collection,
                    [],
                    issues_by_series=issues_by_series,
                    values_by_series=values_by_series,
                    grades=grades,
                )
            )
            continue
        for member in member_rows:
            series = series_by_numista_id.get(member.numista_series_id)
            profiles.append(
                build_profile(
                    collection,
                    [series] if series else [],
                    member=member,
                    issues_by_series=issues_by_series,
                    values_by_series=values_by_series,
                    grades=grades,
                )
            )
    return profiles


def merge_profiles(
    member_profiles: list[CoinCollectionProfile],
    series_by_numista_id: dict[int, CoinSeries],
) -> list[CoinCollectionProfile]:
    """把成员级画像合并成**每个收藏系列一张卡**，唯一键是 (年份, 名称)。

    同一 (年份, 名称) 在多个成员间取发行量最大的一条；同年不同名称各留一枚，
    因此「中国 · 珍稀动物系列 · 5 元」的 10 枚不会塌缩成 6 个年份。
    """
    grouped: dict[int, list[CoinCollectionProfile]] = {}
    order: list[int] = []
    for profile in member_profiles:
        key = profile.collection.id or 0
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(profile)

    profiles: list[CoinCollectionProfile] = []
    for key in order:
        rows = grouped[key]
        collection = rows[0].collection
        year_specs: dict[tuple[int, str], CoinYearSpec] = {}
        owned_keys: set[tuple[int, str]] = set()
        upgrade_keys: set[tuple[int, str]] = set()
        for row in rows:
            name = (row.theme or "").strip()
            for (year, _), spec in row.year_specs.items():
                spec_key = (year, name)
                current = year_specs.get(spec_key)
                if current is None or (spec.mintage or 0) > (current.mintage or 0):
                    year_specs[spec_key] = spec
            owned_keys |= {(year, name) for year in row.owned_years}
            upgrade_keys |= {(year, name) for year in row.upgrade_years}
        year_specs = dict(sorted(year_specs.items()))
        years = sorted({year for year, _ in year_specs})
        owned = sorted({year for year, _ in owned_keys})
        owned_set = set(owned)
        upgrade = sorted({year for year, _ in upgrade_keys if year in owned_set})
        missing = [year for year in years if year not in owned_set]
        # 推荐用的年份 → 名称：同一年取发行量最大条目的名称
        year_names: dict[int, str] = {}
        for (year, name), spec in sorted(
            year_specs.items(), key=lambda item: (item[0][0], -(item[1].mintage or 0))
        ):
            if name:
                year_names.setdefault(year, name)
        member_series_ids = list(
            dict.fromkeys(series_id for row in rows for series_id in row.member_series_ids)
        )
        member_series = [
            series_by_numista_id[series_id]
            for series_id in member_series_ids
            if series_id in series_by_numista_id
        ]
        eligible = [series for series in member_series if is_eligible_series(series)]
        size_mm = _collection_size(collection, eligible)
        commemorated_topic = next(
            (
                (row.commemorated_topic or "").strip()
                for row in rows
                if (row.commemorated_topic or "").strip()
            ),
            "",
        )
        issue_series = next(
            (
                (row.issue_series or "").strip()
                for row in rows
                if (row.issue_series or "").strip()
            ),
            "",
        )
        issue_theme = next(
            (
                (row.issue_theme or "").strip()
                for row in rows
                if (row.issue_theme or "").strip()
            ),
            "",
        )
        profiles.append(
            CoinCollectionProfile(
                collection=collection,
                member_series_ids=member_series_ids,
                min_year=years[0] if years else None,
                max_year=years[-1] if years else None,
                years=years,
                owned_years=owned,
                missing_years=missing,
                upgrade_years=upgrade,
                year_specs=year_specs,
                size_mm=size_mm,
                size_band=size_band(size_mm),
                year_names=year_names,
                commemorated_topic=commemorated_topic,
                issue_series=issue_series,
                issue_theme=issue_theme,
                is_commemorative=any(
                    is_commemorative_object(series.object_type) for series in eligible
                ),
                owned_keys=owned_keys,
                upgrade_keys=upgrade_keys,
            )
        )
    return profiles


def build_profiles(
    collections: list[CoinCollectionSeries],
    members: list[CoinCollectionMember],
    series_by_numista_id: dict[int, CoinSeries],
    *,
    issues_by_series: dict[int, list[CoinIssue]],
    values_by_series: dict[int, list[CoinIssueValues]],
    grades: list[CoinPurchaseGrade],
) -> list[CoinCollectionProfile]:
    """批量组装画像：**每个收藏系列一张卡**，按 (年份, 名称) 唯一。"""
    return merge_profiles(
        build_member_profiles(
            collections,
            members,
            series_by_numista_id,
            issues_by_series=issues_by_series,
            values_by_series=values_by_series,
            grades=grades,
        ),
        series_by_numista_id,
    )


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


def _load_profile_inputs() -> tuple[
    list[CoinCollectionSeries],
    list[CoinCollectionMember],
    dict[int, CoinSeries],
    dict[int, list[CoinIssue]],
    dict[int, list[CoinIssueValues]],
    list[CoinPurchaseGrade],
]:
    """读画像所需的全部输入（两种粒度共用，避免重复读库）。"""
    collections = repository.list_coin_collection_series()
    if not collections:
        return [], [], {}, {}, {}, []
    members = repository.list_coin_collection_members()
    member_ids = {member.numista_series_id for member in members}
    series_cn = {
        series.numista_id: series
        for series in repository.list_coin_series(cn=True)
        if series.numista_id in member_ids
    }
    issues_by_series, values_by_series = _load_issue_data(list(series_cn.values()))
    return (
        collections,
        members,
        series_cn,
        issues_by_series,
        values_by_series,
        repository.list_coin_purchase_grades(),
    )


def load_profiles() -> list[CoinCollectionProfile]:
    """从库里读全量数据并组装**收藏系列级**画像（页面 / 推荐 / 管道共用）。"""
    collections, members, series_cn, issues, values, grades = _load_profile_inputs()
    if not collections:
        return []
    return build_profiles(
        collections,
        members,
        series_cn,
        issues_by_series=issues,
        values_by_series=values,
        grades=grades,
    )


def load_member_profiles() -> list[CoinCollectionProfile]:
    """成员级画像：`/coins/collections` 编辑页按成员展示与分组用。"""
    collections, members, series_cn, issues, values, grades = _load_profile_inputs()
    if not collections:
        return []
    return build_member_profiles(
        collections,
        members,
        series_cn,
        issues_by_series=issues,
        values_by_series=values,
        grades=grades,
    )


def load_explore_profiles() -> list[CoinCollectionProfile]:
    """第三层「跨系列相似推荐」的探索画像：来自候选池（候选 + 采纳，忽略除外）。

    候选池由 `app/coins/explore.py` 扫描外部铜币目录产生（spec §14.17.6）。
    """
    from app.coins import explore

    return explore.load_candidate_profiles()

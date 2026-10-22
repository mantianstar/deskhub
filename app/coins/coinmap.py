"""拍品 → Numista 币种映射（plan/coins.md §18.18.1 P0-4 / §18.18.5）。

「按币种聚合」不能靠标题猜（spec §14.18.2-14.18.6 的多数分析都卡在币种映射）：

1. 国家 / 年份优先取拍品属性 `attrs`（落库在 `coin_lots.tags` 的「国家地区 / 发行年份」）；
2. 属性缺失时从标题抽「国家 + 4 位年份 + 面值」；
3. 两边都过 `grades.normalize_country` / `normalize_denomination` 统一口径；
4. 用 `coin_series` 的 `issuer` + 年份区间 + `denomination` 匹配，**唯一命中才写单版**；
   多候选只落候选集与置信度，`series/issue_numista_id` 留空（宁缺勿错）。

不改 `match_score`：映射是独立维度，与推荐匹配互不影响。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app import repository
from app.coins.grades import normalize_country, normalize_denomination, normalize_year

# 拍品属性里的键名（attrs 落库后就是 tags）
COUNTRY_KEYS = ("国家地区", "国家")
YEAR_KEYS = ("发行年份", "年份")

# 只认 4 位公历年；年号纪年（明治十五年…）在 grades 里是年号数字，无法直接对公历区间，
# 宁可留空也不猜（宁缺勿错）。
_YEAR_RE = re.compile(r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)")
_WHITESPACE_RE = re.compile(r"\s+")

# 置信度：属性路径比标题路径可靠；单版唯一再 +0.05
_CONFIDENCE_ATTRS = 0.9
_CONFIDENCE_TITLE = 0.7
_CONFIDENCE_ISSUE_BONUS = 0.05


@dataclass(frozen=True)
class SeriesFacts:
    """匹配只需要系列的这几个字段（把归一化放在建目录时做一次）。"""

    numista_id: int
    country: str
    min_year: int | None
    max_year: int | None
    denomination: str  # 已归一化 + 去空白的紧凑形式


@dataclass(frozen=True)
class Catalogue:
    """匹配目录：系列 + 国家词表 + 单版年份索引。"""

    series: tuple[SeriesFacts, ...]
    issuers: tuple[tuple[str, str], ...]  # (标题里找的紧凑词, 归一化国家)，长词优先
    issue_years: dict[int, dict[int, list[int]]]  # 系列号 -> 年份 -> 单版号


@dataclass(frozen=True)
class LotFacts:
    country: str
    years: tuple[int, ...]
    from_attrs: bool


@dataclass(frozen=True)
class LotMapping:
    series_numista_id: int | None
    issue_numista_id: int | None
    match_method: str
    confidence: float
    candidates: tuple[int, ...] = ()


@dataclass(frozen=True)
class MapRunResult:
    lots_total: int
    lots_series_mapped: int
    lots_issue_mapped: int
    by_method: dict[str, int]


_NONE = LotMapping(None, None, "none", 0.0)


def _compact(text: str | None) -> str:
    """去空白，并把分数斜杠统一成 Numista 的 `⁄`（`1/12先令` 与 `1⁄12 先令` 要对上）。"""
    return _WHITESPACE_RE.sub("", text or "").replace("/", "⁄")


def _has_value(value: str, text: str) -> bool:
    """紧凑子串命中，但数字开头/结尾的值不许咬进更长的数字里（`1分` 不咬 `25分`）。"""
    if not value:
        return False
    start = text.find(value)
    while start != -1:
        end = start + len(value)
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        if not (value[0].isdigit() and before.isdigit()) and not (
            value[-1].isdigit() and after.isdigit()
        ):
            return True
        start = text.find(value, start + 1)
    return False


def years_from_text(text: str | None) -> tuple[int, ...]:
    """标题里的 4 位公历年（去重保序）；`1980-1985` 会得到两个年份。"""
    years: list[int] = []
    for match in _YEAR_RE.findall(text or ""):
        year = int(match)
        if year not in years:
            years.append(year)
    return tuple(years)


def _attr(tags: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = (tags.get(key) or "").strip()
        if value:
            return value
    return ""


def lot_facts(title: str, tags: dict[str, str], issuers: tuple[tuple[str, str], ...]) -> LotFacts:
    """拍品事实：国家 / 年份先看属性，再退回标题。"""
    attr_country = normalize_country(_attr(tags, COUNTRY_KEYS))
    attr_years = years_from_text(normalize_year(_attr(tags, YEAR_KEYS)))
    text = _compact(title)
    country = attr_country or _country_in_title(text, issuers)
    years = attr_years or years_from_text(title)
    return LotFacts(country=country, years=years, from_attrs=bool(attr_country or attr_years))


def _country_in_title(text: str, issuers: tuple[tuple[str, str], ...]) -> str:
    for needle, country in issuers:
        if needle and needle in text:
            return country
    return ""


def _in_year_range(series: SeriesFacts, years: tuple[int, ...]) -> bool:
    if series.min_year is None and series.max_year is None:
        return False  # 区间都缺，无法用年份校验
    low = series.min_year if series.min_year is not None else 0
    high = series.max_year if series.max_year is not None else 9999
    return any(low <= year <= high for year in years)


def _filter_by_denomination(candidates: list[SeriesFacts], text: str) -> list[SeriesFacts]:
    """面值必须命中：国家 + 年份只是缩小范围，没写明面值就无法可靠定位到某个系列。

    实测教训：英国系列大多是「1 便士」，只靠国家 + 年份会把「1 弗洛林」「5 先令」
    甚至纸币「1 英镑」都记到便士系列上 —— 宁可不映射。
    """
    if not text:
        return []
    return [item for item in candidates if _has_value(item.denomination, text)]


def match_lot(title: str, tags: dict[str, str], catalogue: Catalogue) -> LotMapping:
    """单条拍品 → 系列 / 单版 / 匹配方式 / 置信度。"""
    facts = lot_facts(title, tags, catalogue.issuers)
    if not facts.country or not facts.years:
        return _NONE

    method = "attrs" if facts.from_attrs else "title"
    candidates = [
        item
        for item in catalogue.series
        if item.country == facts.country and _in_year_range(item, facts.years)
    ]
    if not candidates:
        return _NONE
    candidates = _filter_by_denomination(candidates, _compact(title))
    if not candidates:
        return _NONE

    if len(candidates) > 1:
        ids = tuple(sorted(item.numista_id for item in candidates))
        return LotMapping(
            series_numista_id=None,
            issue_numista_id=None,
            match_method=f"{method}_multi",
            confidence=round(1 / len(candidates), 2),
            candidates=ids,
        )

    series = candidates[0]
    issue_ids: tuple[int, ...] = ()
    if len(facts.years) == 1:
        issue_ids = tuple(catalogue.issue_years.get(series.numista_id, {}).get(facts.years[0], ()))

    base = _CONFIDENCE_ATTRS if facts.from_attrs else _CONFIDENCE_TITLE
    if len(issue_ids) == 1:
        return LotMapping(
            series_numista_id=series.numista_id,
            issue_numista_id=issue_ids[0],
            match_method=method,
            confidence=round(base + _CONFIDENCE_ISSUE_BONUS, 2),
        )
    # 同年多版别（Numista 一个年份常有多个 issue）：系列可信，单版留空并记候选
    return LotMapping(
        series_numista_id=series.numista_id,
        issue_numista_id=None,
        match_method=method,
        confidence=round(base, 2),
        candidates=tuple(sorted(issue_ids)),
    )


# ---------------------------------------------------------------- 落库


def load_catalogue() -> Catalogue:
    """读中文币库（空则退回英文），把归一化与年份索引一次建好。"""
    series_rows = repository.list_coin_series(cn=True)
    cn = True
    if not series_rows:
        series_rows = repository.list_coin_series(cn=False)
        cn = False

    series = tuple(
        SeriesFacts(
            numista_id=row.numista_id,
            country=normalize_country(row.issuer),
            min_year=row.min_year,
            max_year=row.max_year,
            denomination=_compact(normalize_denomination(row.denomination)),
        )
        for row in series_rows
        if row.issuer
    )
    countries = {normalize_country(row.issuer) for row in series_rows if row.issuer}
    issuers = tuple(sorted(((_compact(name), name) for name in countries), key=lambda p: -len(p[0])))

    issue_years: dict[int, dict[int, list[int]]] = {}
    for row in repository.list_coin_issue_years(cn=cn):
        year = row["year"]
        if year is None:
            continue
        issue_years.setdefault(int(row["series_numista_id"]), {}).setdefault(int(year), []).append(
            int(row["issue_numista_id"])
        )
    return Catalogue(series=series, issuers=issuers, issue_years=issue_years)


def _load_tags(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _payload(mapping: LotMapping) -> tuple:
    candidates = (
        json.dumps(list(mapping.candidates), ensure_ascii=False) if mapping.candidates else None
    )
    return (
        mapping.series_numista_id,
        mapping.issue_numista_id,
        mapping.match_method,
        mapping.confidence,
        candidates,
    )


def _row_payload(row) -> tuple:
    confidence = row["confidence"]
    return (
        row["series_numista_id"],
        row["issue_numista_id"],
        row["match_method"],
        None if confidence is None else round(float(confidence), 2),
        row["match_candidates"],
    )


def remap_all(catalogue: Catalogue | None = None) -> MapRunResult:
    """对全部拍品重跑映射并写库（只写有变化的行），返回覆盖率与方式分布。"""
    catalogue = catalogue or load_catalogue()
    updates: list[tuple] = []
    for row in repository.list_coin_lots_for_mapping():
        mapping = match_lot(row["title"], _load_tags(row["tags"]), catalogue)
        payload = _payload(mapping)
        if payload == _row_payload(row):
            continue
        updates.append((*payload, row["id"]))
    repository.update_coin_lot_mappings(updates)

    stats = repository.count_coin_lot_mapping_stats()
    return MapRunResult(
        lots_total=stats["total"],
        lots_series_mapped=stats["series_mapped"],
        lots_issue_mapped=stats["issue_mapped"],
        by_method=repository.count_coin_lot_match_methods(),
    )

"""跨系列探索候选池：从收藏画像出发，到 Numista 目录找尺寸相近的外部铜币（spec §14.17.4）。

数据流：
1. 收藏画像提供种子（尺寸）；
2. 用目录列表页的服务端筛选（类别 / 年份 / 材质 / 直径，参数在 `coins.collection.explore_catalogue`）拉候选；
3. 列表里先按材质铜 / 青铜、尺寸差 ≤ 容差、本地未采集过过滤；
4. 合格项再取详情与年份，写入 `coin_explore_candidates`，状态默认「候选」；
5. 页面采纳时才写 `coin_series[_cn]`，信息页 / 采集管理页随即可见。

职责边界：本模块不依赖 FastAPI；SQL 一律走 `app.repository`。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from app import repository
from app.coins import collection as coin_collection
from app.coins import numista, numista_web, translate
from app.config import Config
from app.models import (
    CoinCollectionProfile,
    CoinCollectionSeries,
    CoinExploreCandidate,
    CoinIssue,
    CoinSeries,
)

logger = logging.getLogger(__name__)

STATUS_CANDIDATE = "candidate"
STATUS_ADOPTED = "adopted"
STATUS_IGNORED = "ignored"


@dataclass(frozen=True)
class ExploreSeed:
    """一个探索种子：收藏系列的 collection_key / 展示名 / 面值关键词 / 尺寸。"""

    key: str
    name: str
    query: str
    size_mm: float


@dataclass(frozen=True)
class ScanResult:
    """一次目录扫描：目录命中条数 / 实际取详情数 / 入池数。"""

    listed: int
    details: int
    stored: int


def build_seeds(profiles: list[CoinCollectionProfile]) -> list[ExploreSeed]:
    """从收藏画像提取种子；没有尺寸或面值无法翻译的系列跳过。"""
    seeds: list[ExploreSeed] = []
    for profile in profiles:
        size_mm = profile.size_mm
        if size_mm is None:
            continue
        try:
            query = numista.translate_denomination(profile.collection.denomination)
        except numista.NumistaError:
            continue
        if not query:
            continue
        seeds.append(
            ExploreSeed(
                key=profile.collection.collection_key,
                name=profile.collection.name,
                query=query,
                size_mm=size_mm,
            )
        )
    return seeds


def _match(
    size_mm: float | None,
    composition: str | None,
    seeds: list[ExploreSeed],
    tolerance_mm: float,
) -> tuple[ExploreSeed, float] | None:
    """尺寸 / 材质是否合格；返回最接近的种子与差值。"""
    if not coin_collection.is_bronze_or_copper(composition):
        return None
    if size_mm is None:
        return None
    best: tuple[ExploreSeed, float] | None = None
    for seed in seeds:
        diff = abs(size_mm - seed.size_mm)
        if diff > tolerance_mm:
            continue
        if best is None or diff < best[1]:
            best = (seed, diff)
    return best


def match_seed(
    info: numista.SeriesInfo, seeds: list[ExploreSeed], tolerance_mm: float
) -> tuple[ExploreSeed, float] | None:
    """候选是否合格：铜 / 青铜 + 与某个种子的尺寸差 ≤ 容差。返回最接近的种子与差值。"""
    return _match(info.size_mm, info.composition, seeds, tolerance_mm)


def _series_from_payload(payload: dict[str, object], fetched_at: str) -> CoinSeries:
    return CoinSeries(
        id=None,
        numista_id=int(payload["numista_id"]),
        title=payload.get("title"),
        url=payload.get("url"),
        object_type=payload.get("object_type"),
        issuer=payload.get("issuer"),
        min_year=payload.get("min_year"),
        max_year=payload.get("max_year"),
        ruler=payload.get("ruler"),
        denomination=payload.get("denomination"),
        size_mm=payload.get("size_mm"),
        weight_g=payload.get("weight_g"),
        thickness_mm=payload.get("thickness_mm"),
        shape=payload.get("shape"),
        composition=payload.get("composition"),
        edge=payload.get("edge"),
        obverse_desc=payload.get("obverse_desc"),
        obverse_lettering=payload.get("obverse_lettering"),
        obverse_picture=payload.get("obverse_picture"),
        reverse_desc=payload.get("reverse_desc"),
        reverse_lettering=payload.get("reverse_lettering"),
        reverse_picture=payload.get("reverse_picture"),
        catalog_no=payload.get("catalog_no"),
        comments=payload.get("comments"),
        mints=list(payload.get("mints") or []),
        fetched_at=fetched_at,
    )


def _issue_from_payload(
    data: dict[str, object], series_id: int, fetched_at: str
) -> CoinIssue:
    return CoinIssue(
        id=None,
        numista_id=int(data["numista_id"]),
        series_id=series_id,
        year=data.get("year"),
        gregorian_year=data.get("gregorian_year"),
        mint=data.get("mint"),
        mint_mark=data.get("mint_mark"),
        variety=data.get("variety"),
        comment=data.get("comment"),
        mintage=data.get("mintage"),
        size_mm=data.get("size_mm"),
        weight_g=data.get("weight_g"),
        thickness_mm=data.get("thickness_mm"),
        edge=data.get("edge"),
        is_proof=data.get("is_proof"),
        is_essai=data.get("is_essai"),
        picture=data.get("picture"),
        catalog_no=data.get("catalog_no"),
        fetched_at=fetched_at,
    )


def load_candidate_profiles() -> list[CoinCollectionProfile]:
    """把候选池（候选 + 采纳，忽略除外）转成 P3 可用的画像。"""
    candidates = repository.list_coin_explore_candidates()
    if not candidates:
        return []
    grades = repository.list_coin_purchase_grades()
    now = repository.utcnow()
    profiles: list[CoinCollectionProfile] = []
    for candidate in candidates:
        series = _series_from_payload(candidate.payload_cn, now)
        issues = [
            _issue_from_payload(item, 0, now)
            for item in candidate.issues
            if item.get("numista_id") is not None
        ]
        row = CoinCollectionSeries(
            id=None,
            collection_key=f"explore:{candidate.numista_id}",
            name=series.title or series.denomination or str(candidate.numista_id),
            country=series.issuer or "",
            ruler=series.ruler,
            denomination=series.denomination or "",
            size_mm=series.size_mm,
            size_band=coin_collection.size_band(series.size_mm),
            created_at=now,
            updated_at=now,
        )
        profile = coin_collection.build_profile(
            row,
            [series],
            issues_by_series={series.numista_id: issues},
            values_by_series={},
            grades=grades,
        )
        if profile.years:
            profiles.append(profile)
    return profiles


def adopt_candidate(numista_id: int) -> bool:
    """采纳：把候选详情（含年份）写入本地系列表，信息页 / 采集管理页随即可见。"""
    candidate = repository.get_coin_explore_candidate(numista_id)
    if candidate is None:
        return False
    now = repository.utcnow()
    series_en = _series_from_payload(candidate.payload, now)
    series_cn = _series_from_payload(candidate.payload_cn, now)
    en_id = repository.upsert_coin_series(series_en)
    cn_id = repository.upsert_coin_series_cn(series_cn)
    for item in candidate.issues:
        if item.get("numista_id") is None:
            continue
        issue = _issue_from_payload(item, en_id, now)
        repository.upsert_coin_issue(issue)
        cn_issue = translate.issue_to_zh(issue)
        cn_issue.series_id = cn_id
        repository.upsert_coin_issue_cn(translate.strip_html_texts(cn_issue))
    repository.set_coin_explore_candidate_status(numista_id, STATUS_ADOPTED)
    return True


async def scan_candidates(
    config: Config,
    *,
    client: numista.NumistaClient | None = None,
    limit: int | None = None,
) -> ScanResult:
    """按配置的目录筛选拉候选，把合格系列写入候选池。

    列表页已经带材质 / 重量 / 直径，先在本地筛掉不合格的，再为剩下的取详情
    （详情接口有月度额度，能少调就少调）。
    """
    profiles = coin_collection.load_profiles()
    seeds = build_seeds(profiles)
    if not seeds:
        return ScanResult(0, 0, 0)

    cap = limit if limit is not None else config.coins.collection.explore_max_details
    tolerance = config.coins.collection.explore_size_tolerance_mm
    filters = config.coins.collection.explore_catalogue
    logger.info(
        "候选池目录筛选：类别 st=%s · 年份 a=%s · 材质 m=%s · 直径 t=%s mm",
        filters.collectible_type,
        filters.year,
        filters.composition_id,
        filters.diameter_mm,
    )
    catalogue = await numista_web.load_catalogue_index(config)

    known_ids = {series.numista_id for series in repository.list_coin_series()}
    known_ids |= {
        series.numista_id for series in repository.list_coin_series(cn=True)
    }
    existing = {
        candidate.numista_id
        for candidate in repository.list_coin_explore_candidates(include_ignored=True)
    }

    picked: list[tuple[int, tuple[ExploreSeed, float]]] = []
    for item in catalogue.items:
        if item.numista_id in known_ids or item.numista_id in existing:
            continue
        matched = _match(item.size_mm, item.composition, seeds, tolerance)
        if matched is None:
            continue
        picked.append((item.numista_id, matched))
    # 尺寸最接近的优先，保证达到单轮详情上限时先取最相关的
    picked.sort(key=lambda pair: pair[1][1])

    if not picked:
        return ScanResult(catalogue.total, 0, 0)

    owned = client is None
    api = client or numista.NumistaClient(config)
    if owned:
        await api.__aenter__()
    details = 0
    stored = 0
    try:
        async with numista._translator(config, None) as translator:
            for type_id, _ in picked:
                if details >= cap:
                    break
                info = await api.type_detail(type_id)
                details += 1
                matched = match_seed(info, seeds, tolerance)
                if matched is None:
                    continue
                seed, diff = matched
                issues = await api.issues(type_id, series=info)
                cn_info = await numista._cn_series_info(info, translator)
                now = repository.utcnow()
                repository.upsert_coin_explore_candidate(
                    CoinExploreCandidate(
                        id=None,
                        numista_id=info.numista_id,
                        seed_key=seed.key,
                        seed_name=seed.name,
                        size_diff=round(diff, 2),
                        status=STATUS_CANDIDATE,
                        payload=asdict(info),
                        payload_cn=asdict(cn_info),
                        issues=[asdict(issue) for issue in issues],
                        verified_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                existing.add(type_id)
                stored += 1
                # 图片走本地缓存（Numista 禁止跨站引用）；失败不影响候选入库
                try:
                    await numista_web.download_series_images(config, numista_id=type_id)
                except numista_web.NumistaPageError as exc:
                    logger.warning("候选 %s 图片缓存失败：%s", type_id, exc)
        logger.info(
            "候选池扫描：目录 %d 条，取详情 %d 个，入池 %d 个",
            catalogue.total,
            details,
            stored,
        )
        return ScanResult(catalogue.total, details, stored)
    finally:
        if owned:
            await api.__aexit__(None, None, None)

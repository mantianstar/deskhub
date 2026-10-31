"""跨系列探索候选池：按目录筛选条件到 Numista 目录找外部铜币（spec §14.17.4）。

数据流：
1. 用目录列表页的服务端筛选（发行方 / 类别 / 年份 / 材质 / 直径，参数在 `coins.collection.explore_catalogue`）拉候选；
2. 列表里只按本地未采集过过滤（不限定材质）；
3. 合格项再取详情与年份，写入 `coin_explore_candidates`，状态默认「候选」；
4. 页面采纳时才写 `coin_series[_cn]`，信息页 / 采集管理页随即可见。

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
class ScanResult:
    """一次目录扫描：目录命中条数 / 实际取详情数 / 入池数。"""

    listed: int
    details: int
    stored: int


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
        series=list(payload.get("series") or []),
        commemorated_topic=payload.get("commemorated_topic"),
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

    只跳过本地已入库 / 已在池的系列，不限定材质；剩下的取详情与年份
    （详情接口有月度额度，能少调就少调）。
    """
    cap = limit if limit is not None else config.coins.collection.explore_max_details
    filters = config.coins.collection.explore_catalogue
    logger.info(
        "候选池目录筛选：发行方 e=%s · 类别 st=%s · 年份 a=%s · 材质 m=%s · 直径 t=%s mm",
        filters.issuer,
        filters.collectible_type,
        filters.year or "不限",
        filters.composition_id if filters.composition_id is not None else "不限",
        filters.diameter_mm or "不限",
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

    picked: list[int] = []
    for item in catalogue.items:
        if item.numista_id in known_ids or item.numista_id in existing:
            continue
        picked.append(item.numista_id)

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
            for type_id in picked:
                if details >= cap:
                    break
                info = await api.type_detail(type_id)
                details += 1
                issues = await api.issues(type_id, series=info)
                cn_info = await numista._cn_series_info(info, translator)
                now = repository.utcnow()
                repository.upsert_coin_explore_candidate(
                    CoinExploreCandidate(
                        id=None,
                        numista_id=info.numista_id,
                        seed_key=None,
                        seed_name=None,
                        size_diff=None,
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
                except Exception as exc:  # 下载失败（含超时、浏览器断连）只跳过图片，不中断扫描
                    logger.warning("候选 %s 图片缓存失败，跳过：%s", type_id, exc)
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

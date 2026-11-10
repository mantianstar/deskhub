"""采集中心「批量方式(页面)」：按筛选条件抓 Numista 目录列表页并入库。

与 `app/coins/ingest.py` 的 4 节点队列无关：这里是一次性的页面抓取，
单飞（同一时刻只允许一个任务），进度只存内存，重启丢失可接受。
取页只走 `numista_web.fetch_catalogue_page`（CDP），不用普通 HTTP（会被 403）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable
from uuid import uuid4

from app import llm, repository
from app.coins import numista as coins_numista
from app.coins import numista_web as coins_numista_web
from app.coins import translate, translate_llm
from app.config import Config, ExploreCatalogueConfig
from app.models import CoinPageExploreItem

logger = logging.getLogger(__name__)

MAX_RESULTS = 100
OVER_LIMIT_MESSAGE = "当前探索结果超过 100 条，请重新筛选条件"
_TRANSLATE_CHUNK = 50  # 分块翻译，块间回报进度

# 货币类型 = `st`（不是 `cu`）
COLLECTIBLE_TYPES: tuple[tuple[str, str], ...] = (
    ("1", "标准流通币"),
    ("2", "流通纪念币"),
    ("3", "非流通币"),
    ("47", "收藏币"),
    ("154", "应急货币"),
    ("5", "地方货币"),
    ("6", "样币"),
)
# 材质 = `m`
COMPOSITIONS: tuple[tuple[str, str], ...] = (
    ("3", "铜"),
    ("5", "青铜"),
    ("4", "黄铜"),
    ("2", "白铜"),
    ("1", "银"),
    ("6", "金"),
    ("9", "钢"),
    ("45", "铝"),
    ("10", "铝青铜"),
    ("bi", "双金属"),
)

# 英文名 → 中文名 → slug：英文名复用词表，避免再维护一份国家名单。
_ENGLISH_TO_SLUG = {
    english: coins_numista.ISSUER_CODES[chinese]
    for english, chinese in translate.GLOSSARY.items()
    if chinese in coins_numista.ISSUER_CODES
}


_SLUG_TO_ZH: dict[str, str] = {}
for _zh_name, _slug in coins_numista.ISSUER_CODES.items():
    _SLUG_TO_ZH.setdefault(_slug, _zh_name)
_SLUG_TO_EN = {slug: english for english, slug in _ENGLISH_TO_SLUG.items()}


def resolve_issuer(raw: str) -> str:
    """中文名 / 英文名 / slug → Numista 发行方 slug；映射不到抛 `ValueError`。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if text in coins_numista.ISSUER_CODES:
        return coins_numista.ISSUER_CODES[text]
    if text in _ENGLISH_TO_SLUG:
        return _ENGLISH_TO_SLUG[text]
    lowered = text.lower()
    slugs = set(coins_numista.ISSUER_CODES.values())
    for candidate in (lowered, lowered.replace(" ", "_"), lowered.replace("-", "_")):
        if candidate in slugs:
            return candidate
    raise ValueError(f"无法识别的国家：{text}")


def normalize_filters(
    *, issuer: str, st: str, year: str, material: str, size: str
) -> dict[str, str]:
    """校验并归一化表单条件；不合法抛 `ValueError`（端点转 400）。"""
    st_value = (st or "1").strip() or "1"
    if st_value not in {value for value, _ in COLLECTIBLE_TYPES}:
        raise ValueError("货币类型不合法")
    material_value = (material or "").strip()
    if material_value and material_value not in {value for value, _ in COMPOSITIONS}:
        raise ValueError("材质不合法")
    return {
        "issuer": resolve_issuer(issuer),
        "st": st_value,
        "year": (year or "").strip(),
        "material": material_value,
        "size": (size or "").strip(),
    }


def catalogue_page_url(*, issuer: str, st: str, year: str, material: str, size: str) -> str:
    """后端按条件重算链接（不信任前端传链接）。"""
    composition: int | str | None = None
    if material:
        composition = int(material) if material.isdigit() else material
    filters = ExploreCatalogueConfig(
        issuer=issuer,
        collectible_type=int(st),
        year=year,
        composition_id=composition,
        diameter_mm=size,
    )
    return coins_numista_web.catalogue_url(filters)


def issuer_options() -> list[str]:
    """国家下拉选项：静态词表 `ISSUER_CODES`（中文名，按字典序）。"""
    return sorted(coins_numista.ISSUER_CODES)


def options() -> dict[str, object]:
    return {
        "issuers": issuer_options(),
        "collectible_types": list(COLLECTIBLE_TYPES),
        "compositions": list(COMPOSITIONS),
    }


# ---------------------------------------------------------------- 后台任务（单飞）

_state: dict[str, object] = {
    "job_id": "",
    "status": "idle",
    "total": 0,
    "parsed": 0,
    "saved": 0,
    "skipped": 0,
    "percent": 0,
    "error_code": "",
    "error_message": "",
    "batch_id": "",
}
_running: asyncio.Task[None] | None = None


def is_running() -> bool:
    return _running is not None and not _running.done()


def status() -> dict[str, object]:
    return dict(_state)


def start(
    config: Config, *, issuer: str, st: str, year: str, material: str, size: str
) -> dict[str, str] | None:
    """起一个后台抓取任务；已有任务在跑返回 `None`（端点转 409）。"""
    global _running
    if is_running():
        return None
    url = catalogue_page_url(
        issuer=issuer, st=st, year=year, material=material, size=size
    )
    job_id = uuid4().hex
    batch_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    _state.update(
        {
            "job_id": job_id,
            "status": "running",
            "total": 0,
            "parsed": 0,
            "saved": 0,
            "skipped": 0,
            "percent": 0,
            "error_code": "",
            "error_message": "",
            "batch_id": batch_id,
        }
    )
    _running = asyncio.create_task(
        _run(config, job_id=job_id, batch_id=batch_id, url=url, issuer=issuer)
    )
    return {"job_id": job_id, "url": url, "batch_id": batch_id}


def _fail(job_id: str, code: str, message: str, *, total: int | None = None) -> None:
    if _state.get("job_id") != job_id:
        return
    _state["status"] = "failed"
    _state["error_code"] = code
    _state["error_message"] = message
    if total is not None:
        _state["total"] = total


async def _run(
    config: Config, *, job_id: str, batch_id: str, url: str, issuer: str = ""
) -> None:
    try:
        _state["percent"] = 5  # 取目录阶段没有细粒度回调，先离开 0%
        html = await coins_numista_web.fetch_catalogue_page(config, url=url)
        page = coins_numista_web.parse_catalogue_index(html)
        total = page.total or len(page.items)
        # 风控：超过上限一行都不写
        if total > MAX_RESULTS:
            _fail(job_id, "over_limit", OVER_LIMIT_MESSAGE, total=total)
            return
        _state["total"] = total
        items = page.items[:MAX_RESULTS]
        _state["percent"] = 10

        def _translated(ratio: float) -> None:
            _state["percent"] = 10 + round(ratio * 40)

        zh = await _texts_zh(config, items, on_progress=_translated)
        _state["percent"] = 50
        for index, item in enumerate(items, start=1):
            inserted = repository.upsert_coin_page_explore_item(
                _row(item, batch_id, zh, issuer_slug=issuer)
            )
            if inserted:
                _state["saved"] = int(_state["saved"]) + 1
            else:
                _state["skipped"] = int(_state["skipped"]) + 1
            _state["parsed"] = index
            _state["percent"] = 50 + round(index * 50 / total) if total else 100
        _state["percent"] = 100
        _state["status"] = "done"
    except Exception as exc:  # noqa: BLE001 - 后台任务，异常只能在这里收掉
        logger.warning("页面探索失败：%s", exc)
        _fail(job_id, "fetch_failed", str(exc))
    finally:
        global _running
        _running = None


# 同一进程内同一个英文短词只翻一次；重启即失效，代价可接受（结果已入库）
_TEXT_CACHE: dict[str, str] = {}


def _issuer_leaf(issuer: str | None) -> str | None:
    """多级国家路径 `India › Baroda, Princely state of` 取末级名。"""
    return (issuer or "").split("›")[-1].strip() or None


def _localized(text: str | None, zh: dict[str, str]) -> str | None:
    """整句译文优先；词表只认整词命中（半截替换会中英混排），都没有保留英文原文。"""
    if not text:
        return text
    return zh.get(text) or translate.GLOSSARY.get(text) or text


async def _texts_zh(
    config: Config, items: list, on_progress: Callable[[float], None] | None = None
) -> dict[str, str]:
    """一批结果的短词（名称 / 国家 / 执政者 / 面值 / 材质）整句一次交大模型翻译。

    词表已整词收录的不进请求；没配 Key 或翻译失败只告警，`_row` 保留英文原文。
    """
    texts: list[str] = []
    for item in items:
        for value in (
            item.title,
            item.ruler,
            item.denomination,
            item.composition,
            _issuer_leaf(item.issuer),
        ):
            leaf = value.strip() if isinstance(value, str) else ""
            if leaf and leaf not in texts and leaf not in translate.GLOSSARY:
                texts.append(leaf)
    pending = [text for text in texts if text not in _TEXT_CACHE]
    if pending:
        try:
            client = llm.get_client(config)
        except llm.LLMError as exc:
            logger.warning("页面探索短词翻译失败，回退内置词表：%s", exc)
            client = None
        # 分块翻译：每块完成回调一次进度，前端百分比在采集途中也能推进
        if client is not None:
            for start in range(0, len(pending), _TRANSLATE_CHUNK):
                chunk = pending[start : start + _TRANSLATE_CHUNK]
                try:
                    _TEXT_CACHE.update(await translate_llm.short_texts_zh(client, chunk))
                except llm.LLMError as exc:
                    logger.warning("页面探索短词翻译失败，回退内置词表：%s", exc)
                    break
                if on_progress is not None:
                    on_progress(min(start + len(chunk), len(pending)) / len(pending))
    return {text: _TEXT_CACHE[text] for text in texts if text in _TEXT_CACHE}


def _row(
    item, batch_id: str, zh: dict[str, str] | None = None, issuer_slug: str = ""
) -> CoinPageExploreItem:
    zh = zh or {}
    issuer, issuer_cn = item.issuer, _localized(_issuer_leaf(item.issuer), zh)
    # 按国家筛选时页面分组头可能只剩货币名（如 `Pataca (1894-date)`），
    # 此时整页都属于筛选国家，直接用筛选值回填国家中英文
    if issuer_slug in _SLUG_TO_ZH:
        issuer = _SLUG_TO_EN.get(issuer_slug) or issuer
        issuer_cn = _SLUG_TO_ZH[issuer_slug]
    return CoinPageExploreItem(
        numista_id=item.numista_id,
        batch_id=batch_id,
        title=item.title,
        title_cn=_localized(item.title, zh),
        issuer=issuer,
        issuer_cn=issuer_cn,
        ruler=item.ruler,
        ruler_cn=_localized(item.ruler, zh),
        denomination=item.denomination,
        denomination_cn=_localized(item.denomination, zh),
        year_text=item.year_text,
        min_year=item.min_year,
        max_year=item.max_year,
        composition=item.composition,
        composition_cn=_localized(item.composition, zh),
        size_mm=item.size_mm,
        source_url=f"https://en.numista.com/catalogue/pieces{item.numista_id}.html",
    )

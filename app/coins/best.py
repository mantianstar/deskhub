"""铜币「好品推荐」：匹配今日外币拍品 → 大模型评级 → 供给页面。

流程（每日调度随 `pipeline.run` 一起跑）：
1. `match_today_lots`：币库（中文）生成「国家 + 年份 + 面值」条件，匹配今日 11010 / 11030 / 11070
   类、非评级盒且标题不含银币 / 纪念币的拍品，命中结果写入 `coin_lot_grades`（pending，幂等）。
2. `grade_candidates`：逐条调用 mimo 做品相评级并落库；每天最多评 50 条（失败不占额度）。
3. `load_best_lots`：页面取今日已评级、分数下界 >= 40 的拍品。
"""

from __future__ import annotations

import logging
import re

from app import repository
from app.coin_grade import prompt as grade_prompt
from app.coin_grade.mimo_client import MimoClient, MimoError
from app.coin_grade.parse import parse_grade
from app.coins.recommend import denomination_variants
from app.config import Config
from app.models import CoinBestLot

logger = logging.getLogger(__name__)

BEST_CATEGORY_PREFIXES = ("11010", "11030", "11070")
BEST_EXCLUDED_TITLE_KEYWORDS = ("银币", "纪念币")
BEST_GRADE_THRESHOLD = 40
BEST_DAILY_GRADE_LIMIT = 50

_YEAR_RE = re.compile(r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)")


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "")


def build_search_index() -> list[dict[str, object]]:
    """币库中文系列 → 搜索条件；缺国家 / 面值 / 年份的系列不参与匹配。"""
    index: list[dict[str, object]] = []
    for series in repository.list_coin_series(cn=True):
        if series.id is None:
            continue
        issuer = _squash(series.issuer)
        denominations = [
            value for value in (_squash(item) for item in denomination_variants(series.denomination or ""))
            if value
        ]
        if not issuer or not denominations:
            continue
        if series.min_year is None and series.max_year is None:
            continue
        low = series.min_year if series.min_year is not None else series.max_year
        high = series.max_year if series.max_year is not None else series.min_year
        index.append(
            {
                "series_id": series.id,
                "issuer": issuer,
                "denominations": denominations,
                "min_year": low,
                "max_year": high,
                "span": int(high) - int(low),
            }
        )
    return index


def _match_series(title: str, index: list[dict[str, object]]) -> int | None:
    """标题同时含国家、面值与区间内年份才算命中；多候选取跨度最小的系列。"""
    compact = _squash(title)
    years = [int(token) for token in _YEAR_RE.findall(compact)]
    if not years:
        return None

    best: tuple[tuple[int, int], int] | None = None
    for entry in index:
        if entry["issuer"] not in compact:
            continue
        if not any(denom in compact for denom in entry["denominations"]):
            continue
        low, high = int(entry["min_year"]), int(entry["max_year"])
        if not any(low <= year <= high for year in years):
            continue
        key = (int(entry["span"]), int(entry["series_id"]))
        if best is None or key < best[0]:
            best = (key, int(entry["series_id"]))
    return best[1] if best else None


def match_today_lots(config: Config) -> int:
    """匹配今日 11010 / 11030 / 11070 类、非评级盒且标题不含银币 / 纪念币的拍品并入队。"""
    lots = repository.list_coin_today_lots(
        config.app.tz,
        category_prefixes=BEST_CATEGORY_PREFIXES,
        ungraded_only=True,
        exclude_title_keywords=BEST_EXCLUDED_TITLE_KEYWORDS,
    )
    if not lots:
        return 0
    index = build_search_index()
    rows = [
        (lot.id, int(series_id))
        for lot in lots
        for series_id in [_match_series(lot.title, index)]
        if series_id is not None
    ]
    return repository.insert_pending_coin_lot_grades(rows)


def grade_candidates(config: Config, *, client: MimoClient | None = None) -> int:
    """对待评级队列调模型并落库；返回本轮成功评级条数。"""
    section = config.coin_grade
    if not section.base_url or not section.model or not section.api_key:
        logger.info("好品推荐：coin_grade 未配置，跳过评级")
        return 0

    remaining = BEST_DAILY_GRADE_LIMIT - repository.count_coin_graded_on_day()
    if remaining <= 0:
        return 0
    pending = repository.list_pending_grade_lots(remaining)
    if not pending:
        return 0

    owned = client is None
    active = client or MimoClient(
        base_url=section.base_url,
        api_key=section.api_key,
        model=section.model,
        temperature=section.temperature,
        timeout_seconds=section.timeout_seconds,
    )
    graded = 0
    try:
        for row in pending:
            lot_id = int(row["lot_id"])
            images = list(row["image_urls"] or [])
            if not images:
                repository.mark_coin_lot_grade_failed(lot_id, "拍品没有图片")
                continue
            series_id = row["series_id"]
            series = (
                repository.get_coin_series_by_id(int(series_id))
                if series_id is not None
                else None
            )
            if series is None:
                repository.mark_coin_lot_grade_failed(lot_id, "系列不存在")
                continue
            try:
                raw = active.grade(
                    system=grade_prompt.SYSTEM_PROMPT,
                    user=grade_prompt.build_user_prompt(series),
                    images=images[:2],
                )
            except MimoError as exc:
                repository.mark_coin_lot_grade_failed(lot_id, str(exc))
                logger.warning("好品推荐：拍品 %s 评级失败：%s", lot_id, exc)
                continue
            except Exception as exc:  # 单条失败不影响后续
                repository.mark_coin_lot_grade_failed(lot_id, f"{type(exc).__name__}: {exc}")
                logger.warning("好品推荐：拍品 %s 评级异常：%s", lot_id, exc)
                continue
            repository.save_coin_lot_grade(
                lot_id, parse_grade(raw), model_raw=raw, model=section.model
            )
            graded += 1
    finally:
        if owned:
            active.close()
    return graded


def load_best_lots(config: Config, *, series: str = "") -> list[CoinBestLot]:
    """今日分数 >= XF40 的好品；`series` 为收藏系列 key 时只取该系列成员。"""
    return repository.list_best_grade_lots(
        config.app.tz,
        min_score=BEST_GRADE_THRESHOLD,
        collection_key=series or None,
    )

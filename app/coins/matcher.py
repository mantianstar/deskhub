"""画像匹配：拍品名 → 命中标签 + 匹配分（spec §14.4 / plan §18.6）。

v1 口径：
- 只用「值能出现在拍品名里」的维度做匹配（国家/年代/面值/品类/品相）；`shop` / `price_band`
  不在其中（拍品名里没有它们），它们是画像页的分析维度。
- 命中 = 标签值（归一化后）是拍品名（归一化后）的子串。
- 匹配分 = 所有命中标签 `weight` 之和（权重是**维度内归一化**的，见 profile.aggregate），
  `>= threshold` 记为推荐。这样单靠一个高频国家词不容易顶穿阈值，需要多维度同时命中。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app import models, repository
from app.config import Config

# 能对拍品名做匹配的维度
MATCHABLE_DIMENSIONS = ("country", "era", "denomination", "category", "condition")

# 「够具体」的维度：报价前**必须**至少命中其中一个。
# 理由（2026-10-12 实测）：只靠 国家/年代 拼出来的关键词太泛 ——
#   - 关键词只剩「1961」→ 抓到一堆不相干的 1961 年币；
#   - 「巴拿马 1967」→ 把「精制银套币（当前价 ¥330）」报成 ¥2（匹配到廉价单枚币）。
# 宁可标「关键词太泛，不报价」，也不给一个不可信的价（spec §14.6 的红线精神）。
SPECIFIC_DIMENSIONS = ("denomination", "category")

# 纯营销/修饰词：归一化时删掉，避免它们干扰子串命中。
# **不要**把品相/包浆类词（如「原光」「巧克力包浆」）放进来 —— 那些是 condition 维度的值。
MARKETING_WORDS = (
    "包老保真",
    "包老包真",
    "包真",
    "美品",
    "极美品",
    "极品",
    "少见",
    "稀少",
    "无售后",
    "自定义售出",
    "喜欢的拍走",
    "值得收藏",
)

_WHITESPACE = (" ", "\u3000", "\t", "\n", "\r")

# 必须先删长词再删短词，否则「极美品」会被「美品」先咬掉、只剩一个「极」（实测踩到）
_MARKETING_LONGEST_FIRST = tuple(sorted(MARKETING_WORDS, key=len, reverse=True))


@dataclass(frozen=True)
class TagHit:
    dimension: str
    value: str
    weight: float


@dataclass(frozen=True)
class MatchRunResult:
    lots_total: int
    lots_recommended: int
    threshold: float


def normalize_title(title: str) -> str:
    """归一化拍品名：去空白、去纯营销词。"""
    text = title
    for marker in _WHITESPACE:
        text = text.replace(marker, "")
    for word in _MARKETING_LONGEST_FIRST:
        text = text.replace(word, "")
    return text


def _hits_value(value: str, text: str) -> bool:
    """子串命中，但**数字开头/结尾的值不许咬进更长的数字里**。

    实测踩到：画像里的面值标签「5分」会命中「**2**5分」——于是「美国1962年25分」被当成
    「5分」去查价，推荐价直接失真。数字前后是数字时跳过，继续找下一个位置。
    """
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


def match_lot(
    title: str,
    tags: Sequence[models.CoinProfileTag],
    *,
    dimensions: Sequence[str] = MATCHABLE_DIMENSIONS,
) -> list[TagHit]:
    """返回命中的标签（含权重）。只考虑 `enabled=True` 且维度可匹配的标签。"""
    normalized = normalize_title(title)
    if not normalized:
        return []
    hits: list[TagHit] = []
    for tag in tags:
        if not tag.enabled or tag.dimension not in dimensions or not tag.value:
            continue
        if _hits_value(tag.value, normalized):
            hits.append(TagHit(tag.dimension, tag.value, tag.weight))
    return hits


def score_hits(hits: Sequence[TagHit]) -> float:
    """匹配分 = 命中标签权重之和（同一维度多个标签命中就累加）。"""
    return round(sum(hit.weight for hit in hits), 3)


def has_specific_hit(hits: Sequence[TagHit]) -> bool:
    """命中里有没有「够具体」的维度（面值 / 品类）—— 没有就不该报价。"""
    return any(hit.dimension in SPECIFIC_DIMENSIONS for hit in hits)


def build_keyword(hits: Sequence[TagHit], *, max_terms: int = 3) -> str:
    """从命中标签里拼搜索关键词：优先 国家 → 年代 → 面值/品类（spec §14.6）。

    优先序是「最能定位同类拍品」的维度；权重只用于同维度内选代表词。
    """
    by_dimension: dict[str, TagHit] = {}
    for hit in hits:
        current = by_dimension.get(hit.dimension)
        if current is None or hit.weight > current.weight:
            by_dimension[hit.dimension] = hit

    ordered: list[str] = []
    for dimension in ("country", "era", "denomination", "category"):
        hit = by_dimension.get(dimension)
        if hit and hit.value not in ordered:
            ordered.append(hit.value)
    return " ".join(ordered[:max_terms])


def rematch_all(config: Config, tags: Sequence[models.CoinProfileTag] | None = None) -> MatchRunResult:
    """对库里全部拍品重算匹配分与推荐位（画像改动后当天即可重算，不必重抓）。

    未命中任何标签的拍品 `match_score=0` 且 `is_recommended=0`。
    """
    active_tags = list(tags) if tags is not None else repository.list_coin_profile_tags(only_enabled=True)
    threshold = config.coins.match_threshold
    lots = repository.list_coin_lots_for_matching()

    recommended = 0
    for lot in lots:
        hits = match_lot(lot.title, active_tags)
        score = score_hits(hits)
        flag = score >= threshold
        if flag:
            recommended += 1
        if lot.id is not None:
            repository.update_coin_lot_match(lot.id, score, flag)
    return MatchRunResult(lots_total=len(lots), lots_recommended=recommended, threshold=threshold)

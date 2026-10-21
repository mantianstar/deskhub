"""全局拍品搜索：FTS5 候选 + 推荐标签 + 个性化排序（spec/coins.md §14.7）。

排序权重：精准匹配 > 相似推荐 > 关注店铺 > 普通拍品，同层按截拍时间。
未配置收藏画像时沿用入库的标签匹配结果（与拍场页同一口径）。
"""

from __future__ import annotations

from app import repository
from app.coins import collection as collection_module
from app.coins import recommend as recommend_module
from app.config import Config
from app.models import CoinSearchLot

# 先取一批候选，再在 Python 侧做个性化排序（推荐匹配是纯函数，300 条足够快）
SEARCH_CANDIDATES = 300


def search_lots(
    cfg: Config,
    *,
    keyword: str = "",
    price_min: float | None = None,
    price_max: float | None = None,
) -> list[CoinSearchLot]:
    lots = repository.search_coin_lots(
        keyword,
        price_min=price_min,
        price_max=price_max,
        auction_from=repository.local_day_bounds(cfg.app.tz)[0],
        limit=SEARCH_CANDIDATES,
    )
    labels = _match_labels(cfg, lots)
    for lot in lots:
        if labels is not None:
            lot.match_label = labels.get(lot.id, "")
        elif lot.is_recommended:
            lot.match_label = "precise"
    return sorted(lots, key=_rank_key)


def _match_labels(cfg: Config, lots: list[CoinSearchLot]) -> dict[int, str] | None:
    """拍品 → 精准 / 相似标签；没有收藏画像时返回 None（由调用方回退标签匹配）。"""
    profiles = collection_module.load_profiles()
    if not profiles:
        return None
    base_items = recommend_module.build_recommendations(
        profiles, explore_profiles=[], config=cfg.coins.collection
    )
    base_matches = recommend_module.match_lots(base_items, lots)
    items = recommend_module.build_recommendations(
        profiles,
        explore_profiles=collection_module.load_explore_profiles(),
        search_results=len(base_matches),
        similar_lots=lots,
        exclude_lot_ids=set(base_matches),
        config=cfg.coins.collection,
    )
    matches = recommend_module.match_lots(items, lots)
    return {
        lot_id: "precise" if match.recommendation.priority in (1, 2) else "similar"
        for lot_id, match in matches.items()
    }


def _rank_key(lot: CoinSearchLot) -> tuple[int, str, int]:
    if lot.match_label == "precise":
        rank = 0
    elif lot.match_label == "similar":
        rank = 1
    elif lot.shop_followed:
        rank = 2
    else:
        rank = 3
    return rank, lot.auction_at or "9999", lot.id

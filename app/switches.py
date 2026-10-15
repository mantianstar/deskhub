"""功能开关：控制各类任务是否**自动执行**（管理页 /control 启停，重启后保留）。

三件事在这里定死，别处不要再各写一套：

- 开关只管「每天 08:00 的定时任务」；管理页上的「立即运行」是手动触发，**忽略开关**
  （临时跑一次不该先去改开关）。
- 启动补拉已取消（见 `app/scheduler.py`）：重启不执行任何任务，所以开关与重启无关。
- 库里没有记录的功能用 `DEFAULT_ENABLED` 兜底，**不往库里写默认值** ——
  这样以后改默认值能直接生效。
"""

from __future__ import annotations

from dataclasses import dataclass

from app import repository

KNOWLEDGE_FETCH = "knowledge_fetch"
KNOWLEDGE_SCORE = "knowledge_score"
COINS_COLLECT = "coins_collect"
COINS_PRICE = "coins_price"


@dataclass(frozen=True)
class Feature:
    """一个可独立启停的功能：分组 + 名称 + 一句说明（说明要给到「花不花 token」）。"""

    key: str
    group: str
    label: str
    detail: str


FEATURES: tuple[Feature, ...] = (
    Feature(
        KNOWLEDGE_FETCH,
        "知识聚合",
        "抓取",
        "拉取 RSS/Atom 源并入库；不花 token，但会真的去访问各站点",
    ),
    Feature(
        KNOWLEDGE_SCORE,
        "知识聚合",
        "打分",
        "调 LLM 给未打分的条目打分；**唯一花 token 的功能**",
    ),
    Feature(COINS_COLLECT, "铜币拍卖", "采集", "拉麦稀奇专场与拍品（走 gapi 网关，不花 token）"),
    Feature(COINS_PRICE, "铜币拍卖", "查价", "查站内历史成交；每条间隔 15 秒，四件里最耗时"),
)

# 默认全部启用：重启不再自动跑，但每天 08:00 的定时任务保持原有行为
DEFAULT_ENABLED = True

_BY_KEY = {feature.key: feature for feature in FEATURES}


def is_known(key: str) -> bool:
    return key in _BY_KEY


def is_enabled(key: str) -> bool:
    """定时任务执行前问它。未知 key 一律当「未启用」，避免拼错 key 反而静默跑起来。"""
    if not is_known(key):
        return False
    return repository.feature_switch_states().get(key, DEFAULT_ENABLED)


def toggle(key: str) -> bool:
    """取反并落库，返回新状态；未知 key 抛 `ValueError`。"""
    if not is_known(key):
        raise ValueError(f"未知功能开关：{key}")
    new_value = not is_enabled(key)
    repository.set_feature_switch(key, new_value)
    return new_value


def snapshot() -> list[dict[str, object]]:
    """管理页用：开关定义 + 当前状态。"""
    states = repository.feature_switch_states()
    return [
        {
            "key": feature.key,
            "group": feature.group,
            "label": feature.label,
            "detail": feature.detail,
            "enabled": states.get(feature.key, DEFAULT_ENABLED),
        }
        for feature in FEATURES
    ]

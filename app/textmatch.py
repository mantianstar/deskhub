"""文本匹配小工具：归一化 + 带数字边界的子串命中（口径同 spec §14.11）。

两处用到，且必须是**同一口径**（否则「本地筛出来的」和「采集挡住的」会对不上）：

- `repository.search_coin_series`：本地筛面值 / 材质（中文库值 vs 用户输入）；
- `app.coins.numista.collect_series`：采集时筛面值 / 材质（英文接口值 vs 查询关键词）。

归一化做三件事：去全部空白、转小写、把常见 Unicode 分数符号展开成英文词
（Numista 标题写 `½ Penny`，用户在框里可能写 `half penny`，展开后两边才对得上）。
"""

from __future__ import annotations

import re
from functools import lru_cache

# 常见 Unicode 分数 → 英文词（Numista 用符号，用户习惯写词）
_FRACTIONS = {
    "½": "half",
    "¼": "quarter",
    "¾": "three quarters",
    "⅓": "one third",
    "⅔": "two thirds",
}


@lru_cache(maxsize=16384)
def normalize(text: str | None) -> str:
    """归一化：去空白 + 转小写 + 展开分数符号（纯函数，结果可缓存）。"""
    result = re.sub(r"\s+", "", text or "").lower()
    for symbol, word in _FRACTIONS.items():
        result = result.replace(symbol, word)
    return result


def contains(value: str | None, needle: str) -> bool:
    """归一化后做子串命中；空关键词视为「不限」，直接命中。

    关键词以数字开头 / 结尾时，不许咬进更长的数字（同 spec §14.11）：
    `1便士` 不得命中 `11便士`，`5分` 不得命中 `25分`。
    """
    haystack = normalize(value)
    key = normalize(needle)
    if not key:
        return True
    start = 0
    while True:
        index = haystack.find(key, start)
        if index < 0:
            return False
        end = index + len(key)
        left_ok = not (key[0].isdigit() and index > 0 and haystack[index - 1].isdigit())
        right_ok = not (key[-1].isdigit() and end < len(haystack) and haystack[end].isdigit())
        if left_ok and right_ok:
            return True
        start = index + 1

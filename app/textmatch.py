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


_CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 一个搜索词：CJK / 字母 / 数字连续排列；空白与标点分隔词项
_FTS_TERM = re.compile(r"[0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def fts_text(*parts: str | None) -> str:
    """拼 FTS5 索引文本：CJK 逐字加空格（unicode61 不切中文），西文原样。"""
    text = " ".join(part for part in parts if part)
    return _CJK_CHAR.sub(lambda match: f" {match.group(0)} ", text)


def fts_query(keyword: str | None) -> str:
    """用户输入 → FTS5 MATCH 表达式。

    含 CJK 的词项整体作为**相邻短语**（`"1 便 士"`），数字不按前缀，
    避免 `1便士` 命中 `6便士` 或标题里的年份 `1901`；纯字母仍按前缀匹配。
    """
    tokens: list[str] = []
    for term in _FTS_TERM.findall(keyword or ""):
        if _CJK_CHAR.search(term):
            spaced = " ".join(_CJK_CHAR.sub(lambda m: f" {m.group(0)} ", term).split())
            tokens.append(f'"{spaced}"')
        elif term.isdigit():
            tokens.append(f'"{term}"')
        else:
            tokens.append(f"{term.lower()}*")
    return " AND ".join(tokens)


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

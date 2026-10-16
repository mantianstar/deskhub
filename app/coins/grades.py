"""历史购买品相录入：年份 / 面值按 Numista 口径统一 + 组合合并（spec §14.16）。

历史订单里的 `era` / `denomination` 是 LLM 从订单名里抄的**原样片段**，形态很杂
（「1910」与「1910年」并存、「1便士」与「一分」并存）。本模块把它们统一成与 Numista 中文表一致的口径：

- **年份**：去掉「年 / 年度」后缀只留数字（`1910年` → `1910`）；年号（`康德四年` / `明治八年`）
  没有公历数字，**原样保留**。
- **面值**：数字转成阿拉伯数字、与单位之间加一个空格（`1便士` → `1 便士`、`一分` → `1 分`、
  `半便士` → `半 便士`、`二十文` → `20 文`）。认不出前导数字的（如 `当制钱十文`）原样保留。
- **国家**：去掉殖民时期的前缀（`英属新西兰` → `新西兰`、`英属澳大利亚` → `澳大利亚`），
  与 Numista 的发行方命名对齐。

统一后的值是**组合的去重键**（「1910年」与「1910」合并成一行），也是落库 `coin_purchase_grades`
的键；`coin_orders` 本身一行不改（那是订单级原料，见 spec §14.16）。

纯函数、不依赖 FastAPI 与数据库；SQL 一律在 `app/repository.py`。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app import textmatch
from app.models import CoinOrder, CoinOrderCombo, CoinPurchaseGrade

# 中文数字（含常见异体与「大写」写法，如壹 / 贰 / 伍）与位权；只处理 ≤9999 的写法，够本数据集用
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2, "貳": 2,
    "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6,
    "陸": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}

# 前导分数符号 → Numista 中文口径（与 `translate.GLOSSARY` 的 ½→半、¼→四分之一 一致）
_LEADING_FRACTION = {"½": "半", "¼": "四分之一", "¾": "四分之三"}

# 前导数字：整数 / 小数 / 「1/4」这类分数，可带尾随 ½¼¾（如 `2½盾`）
_LEADING_ARABIC = re.compile(r"^\s*(\d+(?:\.\d+)?(?:\s*/\s*\d+)?[½¼¾]?)")
_LEADING_CN = re.compile(r"^\s*([零〇一二两三四五六七八九十百千壹贰貳叁肆伍陆陸柒捌玖拾佰仟]+)")
_TRAILING_YEAR_SUFFIX = re.compile(r"\s*年度?\s*$")


def _cn_numeral_to_int(text: str) -> int | None:
    """中文数字 → 整数（`十` / `十二` / `二十` / `二十五` / `一百` / `五千`）；不可解析返回 None。"""
    if not text:
        return None
    total = 0
    section = 0
    seen = False
    for char in text:
        if char in _CN_DIGITS:
            section = _CN_DIGITS[char]
            seen = True
        elif char in _CN_UNITS:
            section = (section or 1) * _CN_UNITS[char]
            total += section
            section = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + section


# 殖民时期的「X属」前缀 → 去掉，与 Numista 的发行方命名对齐（英属新西兰 → 新西兰）
_COLONIAL_PREFIXES = (
    "英属", "法属", "荷属", "葡属", "德属", "西属", "美属", "日属",
    "意属", "比属", "丹属", "俄属", "奥属",
)


def normalize_country(value: str | None) -> str:
    """国家：去「X属」殖民前缀（`英属新西兰` → `新西兰`）；去掉后会变空则原样保留。"""
    text = (value or "").strip()
    for prefix in _COLONIAL_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            return text[len(prefix) :].strip()
    return text


def normalize_year(value: str | None) -> str:
    """年份：去「年 / 年度」后缀只留纯数字；年号（无公历数字）原样保留。"""
    text = (value or "").strip()
    if not text:
        return text
    stripped = _TRAILING_YEAR_SUFFIX.sub("", text)
    return stripped if stripped.isdigit() else text


def normalize_denomination(value: str | None) -> str:
    """面值：前导数字转阿拉伯数字、与单位之间加一个空格；认不出前导数字的原样保留。"""
    text = (value or "").strip()
    if not text:
        return text

    if text.startswith("半"):  # Numista 的 ½ Penny 中文口径：半 便士
        rest = text[1:].strip()
        return f"半 {rest}" if rest else text

    if text[0] in _LEADING_FRACTION:  # ½便士 / ¼安娜 → 半 便士 / 四分之一 安娜
        rest = text[1:].strip()
        word = _LEADING_FRACTION[text[0]]
        return f"{word} {rest}" if rest else word

    match = _LEADING_ARABIC.match(text)
    if match:
        rest = text[match.end() :].strip()
        return f"{match.group(1)} {rest}" if rest else match.group(1)

    match = _LEADING_CN.match(text)
    if match:
        number = _cn_numeral_to_int(match.group(1))
        if number is not None:
            rest = text[match.end() :].strip()
            return f"{number} {rest}" if rest else str(number)

    return text


def normalize_key(country: str | None, era: str | None, denomination: str | None) -> tuple[str, str, str]:
    return (
        normalize_country(country),
        normalize_year(era),
        normalize_denomination(denomination),
    )


def build_combos(
    orders: list[CoinOrder],
    grades: list[CoinPurchaseGrade],
) -> list[CoinOrderCombo]:
    """历史订单按「国家 + 年份 + 面值」（统一后）去重，LEFT JOIN 已录品相。

    只取三者齐全的组合（缺一的行拼不出可辨认的币种，见 spec §14.16）；
    已录品相但**没有对应订单**的组合（手工新增的）也作为一行列出（`order_count = 0`）。
    """
    counts: dict[tuple[str, str, str], int] = {}
    for order in orders:
        key = normalize_key(order.country, order.era, order.denomination)
        if not all(key):
            continue
        counts[key] = counts.get(key, 0) + 1

    graded: dict[tuple[str, str, str], str] = {}
    for grade in grades:
        key = normalize_key(grade.country, grade.era, grade.denomination)
        if all(key):
            graded[key] = grade.grade

    return [
        CoinOrderCombo(
            country=key[0],
            era=key[1],
            denomination=key[2],
            order_count=counts.get(key, 0),
            grade=graded.get(key),
        )
        for key in sorted(set(counts) | set(graded))
    ]


def filter_combos(
    combos: list[CoinOrderCombo],
    *,
    country: str | None = None,
    era: str | None = None,
    denomination: str | None = None,
    grade: str | None = None,
) -> list[CoinOrderCombo]:
    """按国家 / 年份 / 面值 / 品相筛（国家 / 年份 / 面值先按同一口径统一；宽松包含匹配，口径同 `app.textmatch`）。

    `grade` 空 = 不限，其余按 好 / 普通 / 差 精确相等。
    """
    needle_country = normalize_country(country)
    needle_era = normalize_year(era)
    needle_denomination = normalize_denomination(denomination)
    needle_grade = (grade or "").strip()
    return [
        combo
        for combo in combos
        if textmatch.contains(combo.country, needle_country)
        and textmatch.contains(combo.era, needle_era)
        and textmatch.contains(combo.denomination, needle_denomination)
        and (not needle_grade or combo.grade == needle_grade)
    ]


def countries_of(combos: list[CoinOrderCombo]) -> list[str]:
    """国家候选（页面 `<datalist>`）。"""
    return sorted({combo.country for combo in combos})


def denominations_of(combos: list[CoinOrderCombo]) -> list[str]:
    """面值候选（页面 `<datalist>`，已是统一后的写法，如 `1 便士`）。"""
    return sorted({combo.denomination for combo in combos})


def order_ids_for_combo(
    rows: Iterable[tuple[int, str | None, str | None, str | None]],
    country: str | None,
    era: str | None,
    denomination: str | None,
) -> list[int]:
    """从 `repository.list_coin_order_keys()` 的结果里挑出属于该组合（**统一后**）的订单 id。

    删「历史错误数据」整行时用：库里存的是**原始**写法（`1910年` / `一分`），
    所以必须逐个按同一口径统一后再比对，不能直接按列相等去删。
    """
    key = normalize_key(country, era, denomination)
    return [
        row_id
        for row_id, raw_country, raw_era, raw_denomination in rows
        if normalize_key(raw_country, raw_era, raw_denomination) == key
    ]

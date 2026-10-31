"""历史购买品相录入：年份 / 面值按 Numista 口径统一 + 组合合并（spec §14.16）。

历史订单里的 `era` / `denomination` 是 LLM 从订单名里抄的**原样片段**，形态很杂
（「1910」与「1910年」并存、「1便士」与「一分」并存）。本模块把它们统一成与 Numista 中文表一致的口径：

- **年份**：去掉「年 / 年度」后缀只留数字（`1910年` → `1910`）；年号纪年取年号后的年份数字
  （`明治十五年` → `15`、`康德元年` → `1`），与 Numista 的 `year` 对齐；只有年号没有年份数字的
  （`光绪`）**原样保留**。
- **面值**：数字转成阿拉伯数字、与单位之间加一个空格（`1便士` → `1 便士`、`一分` → `1 分`、
  `半便士` → `半 便士`、`二十文` → `20 文`）；分数斜杠统一成 Numista 的 `⁄`（U+2044，
  `1/12先令` → `1⁄12 先令`）。认不出前导数字的（如 `当制钱十文`）原样保留。
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

# 前导分数符号 → 展示口径（`¼` 与系列库的 1⁄4 安娜 统一）
_LEADING_FRACTION = {"½": "半", "¼": "1⁄4", "¾": "四分之三"}

# 前导数字：整数 / 小数 / 「1/4」「1⁄4」这类分数，可带尾随 ½¼¾（如 `2½盾`）
_LEADING_ARABIC = re.compile(r"^\s*(\d+(?:\.\d+)?(?:\s*[/⁄]\s*\d+)?[½¼¾]?)")
_LEADING_CN = re.compile(r"^\s*([零〇一二两三四五六七八九十百千壹贰貳叁肆伍陆陸柒捌玖拾佰仟]+)")
_TRAILING_YEAR_SUFFIX = re.compile(r"\s*年度?\s*$")
# 年号纪年末尾的年份数字（`明治十五年` / `民国31年` / `康德元年`）；`元` 表示第一年
_TRAILING_YEAR_NUMBER = re.compile(
    r"([0-9]+|[零〇一二两三四五六七八九十百千壹贰貳叁肆伍陆陸柒捌玖拾佰仟]+|元)$"
)

# 日本年号起始公历年：品相 / 画像按公历年关联，避免「明治6年」与「大正6年」都是 6
_JP_ERAS: tuple[tuple[str, int], ...] = (
    ("明治", 1868),
    ("大正", 1912),
    ("昭和", 1926),
    ("平成", 1989),
    ("令和", 2019),
)


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

# 去完殖民前缀后仍与 Numista 发行方命名不一致的别名（泽西岛 → 泽西）
_COUNTRY_ALIASES = {"泽西岛": "泽西", "根西岛": "根西", "印度 - British": "印度"}


_COUNTRY_PAREN = re.compile(r"[（(][^）)]*[）)]")
_CHINA_COUNTRY_NAMES = {"中国", "中华人民共和国", "china", "people's republic of china"}


def normalize_country(value: str | None) -> str:
    """国家：去逗号后的限定语与括号注记（`中国, People's Republic of` → `中国`、
    `多米尼加共和国 (1844-date)` → `多米尼加共和国`），再去「X属」殖民前缀与别名。"""
    text = (value or "").strip()
    text = re.split(r"[,，]", text, maxsplit=1)[0].strip()
    text = _COUNTRY_PAREN.sub("", text).strip()
    for prefix in _COLONIAL_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix) :].strip()
            break
    return _COUNTRY_ALIASES.get(text, text)


def is_china_country(value: str | None) -> bool:
    """国家是否为中国（`normalize_country` 之后判断，兼容中英文写法）。"""
    return normalize_country(value).lower() in _CHINA_COUNTRY_NAMES


def _era_number(text: str) -> int | None:
    """年号后的年份数字（`明治十五年` → 15、`康德元年` → 1）；认不出返回 None。"""
    match = _TRAILING_YEAR_NUMBER.search(text)
    if not match:
        return None
    digits = match.group(1)
    if digits.isdigit():
        return int(digits)
    return _cn_numeral_to_int("一" if digits == "元" else digits)


def _gregorian_from_japanese_era(text: str) -> int | None:
    """日本年号纪年 → 公历年（`明治十五年` → 1882）；非日本年号返回 None。"""
    for name, start in _JP_ERAS:
        index = text.find(name)
        if index < 0:
            continue
        number = _era_number(text[index + len(name) :].strip())
        if number is not None:
            return start + number - 1
    return None


def _to_cn_numeral(number: int) -> str:
    """1~99 → 中文数字（明治六年 / 明治十五年）。"""
    digits = "零一二三四五六七八九"
    if number < 10:
        return digits[number]
    if number < 20:
        return "十" + (digits[number % 10] if number % 10 else "")
    if number < 100:
        return digits[number // 10] + "十" + (digits[number % 10] if number % 10 else "")
    return str(number)


def year_number(value: str | None) -> str:
    """年号纪年的年份数字（`明治十五年` → `15`），供没有公历年的系列回退匹配。"""
    text = (value or "").strip()
    if not text:
        return text
    stripped = _TRAILING_YEAR_SUFFIX.sub("", text)
    if stripped.isdigit():
        return stripped
    number = _era_number(stripped)
    return str(number) if number is not None else text


def normalize_year(value: str | None) -> str:
    """年份：去「年 / 年度」后缀只留数字；日本年号纪年换算成公历年。

    `明治十五年` → `1882`、`大正六年` → `1917`，与系列的 `gregorian_year` 同口径；
    其他年号仍取年份数字（`康德四年` → `4`、`民国六十二年` → `62`）；
    只有年号没有年份数字（`光绪`）或认不出数字的原样保留。
    """
    text = (value or "").strip()
    if not text:
        return text
    stripped = _TRAILING_YEAR_SUFFIX.sub("", text)
    if stripped.isdigit():
        return stripped
    gregorian = _gregorian_from_japanese_era(stripped)
    if gregorian is not None:
        return str(gregorian)
    number = _era_number(stripped)
    return str(number) if number is not None else text


def japanese_era_label(country: str | None, era: str | None) -> str:
    """日本品相行的年号 + 年号年份（`1873` → `明治六年`）；无公历年 / 非日本返回空串。"""
    text = (country or "").strip().lower()
    if "日本" not in text and "japan" not in text:
        return ""
    year = (era or "").strip()
    if len(year) != 4 or not year.isdigit():
        return ""
    value = int(year)
    name, start = "", 0
    for zh, begin in _JP_ERAS:
        if value >= begin:
            name, start = zh, begin
    if not name:
        return ""
    number = value - start + 1
    return f"{name}{'元' if number == 1 else _to_cn_numeral(number)}年"


def normalize_denomination(value: str | None) -> str:
    """面值：前导数字转阿拉伯数字、与单位之间加一个空格；认不出前导数字的原样保留。"""
    text = (value or "").strip().replace("安那", "安娜")
    if not text:
        return text

    if text.startswith("半"):  # Numista 的 ½ Penny 中文口径：半 便士
        rest = text[1:].strip()
        return f"半 {rest}" if rest else text

    if text[0] in _LEADING_FRACTION:  # ½便士 / ¼安娜 → 半 便士 / 1⁄4 安娜
        rest = text[1:].strip()
        word = _LEADING_FRACTION[text[0]]
        return f"{word} {rest}" if rest else word

    if text.startswith("四分之一"):  # 四分之一安娜 → 1⁄4 安娜（与 ¼ 同口径）
        rest = text[len("四分之一") :].strip()
        return f"1⁄4 {rest}" if rest else "1⁄4"

    match = _LEADING_ARABIC.match(text)
    if match:
        number = match.group(1).replace("/", "⁄")  # 分数斜杠统一成 Numista 的 ⁄
        rest = text[match.end() :].strip()
        return f"{number} {rest}" if rest else number

    match = _LEADING_CN.match(text)
    if match:
        number = _cn_numeral_to_int(match.group(1))
        if number is not None:
            rest = text[match.end() :].strip()
            return f"{number} {rest}" if rest else str(number)

    return text


def normalize_key(
    country: str | None,
    era: str | None,
    denomination: str | None,
    theme: str | None = None,
    coin_name: str | None = None,
) -> tuple[str, str, str, str, str]:
    return (
        normalize_country(country),
        normalize_year(era),
        normalize_denomination(denomination),
        (theme or "").strip(),
        (coin_name or "").strip(),
    )


def build_combos(
    orders: list[CoinOrder],
    grades: list[CoinPurchaseGrade],
) -> list[CoinOrderCombo]:
    """历史订单按「国家 + 年份 + 面值」（统一后）去重，LEFT JOIN 已录品相。

    订单侧没有主题 / 名称（恒为空串），品相侧的具名行会另起一行，与旧的空串行并存。
    只取国家 / 年份 / 面值三者齐全的组合（缺一的行拼不出可辨认的币种，见 spec §14.16）；
    已录品相但**没有对应订单**的组合（手工新增的）也作为一行列出（`order_count = 0`）。
    """
    counts: dict[tuple[str, str, str, str, str], int] = {}
    for order in orders:
        key = normalize_key(order.country, order.era, order.denomination)
        if not all(key[:3]):
            continue
        counts[key] = counts.get(key, 0) + 1

    graded: dict[tuple[str, str, str, str, str], str] = {}
    for grade in grades:
        key = normalize_key(grade.country, grade.era, grade.denomination, grade.theme, grade.coin_name)
        if all(key[:3]):
            graded[key] = grade.grade

    return [
        CoinOrderCombo(
            country=key[0],
            era=key[1],
            denomination=key[2],
            order_count=counts.get(key, 0),
            grade=graded.get(key),
            theme=key[3],
            coin_name=key[4],
            era_label=japanese_era_label(key[0], key[1]),
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

"""内置词表翻译（离线、免费、零依赖；spec §14.13 / plan §18.13）。

分工（2026-10-15 起，见 plan §18.13.4）：本模块管**枚举类字段**（大类 / 形状 / 材质 / 币边 /
国家 / 执政者 / 面值 / 造币厂）——命中词表后精确成中文，也是采集时「按面值 / 材质过滤」的依据；
**自由长文本**（系列名称 / 正反面描述 / 备注）改由大模型翻译（`app/coins/translate_llm.py`），
本模块对它们做**术语替换**并充当大模型不可用时的**回退结果**（没配 Key / 调用失败 → 用这里的译文）。

实现：一份 `GLOSSARY`（英文 → 中文），按**词长降序**做「整词」替换
（前后不挨字母才算命中，避免 `Mint` 咬进 `Mints` / `Mintmark`）。所以只要把**整句**
（如 `Standard circulation coins`）也放进词表，长短语会先于单词命中。
"""

from __future__ import annotations

import html
import re
from dataclasses import replace
from functools import lru_cache
from typing import TypeVar

from app.models import CoinIssue, CoinSeries

T = TypeVar("T")

# 英文 → 中文。整句优先：同一段里「长词」先替换（见 _GLOSSARY_LONGEST_FIRST）。
GLOSSARY: dict[str, str] = {
    # —— 大类（object_type）——
    "Standard circulation coins": "标准流通币",
    "Standard circulation coin": "标准流通币",
    "Non-circulating commemorative coins": "非流通纪念币",
    "Non-circulating coins": "非流通币",
    "Non-circulating coin": "非流通币",
    "Circulating commemorative coins": "流通纪念币",
    "Circulating commemorative coin": "流通纪念币",
    "Bullion coins": "投资币",
    "Fantasy issues": "臆造币",
    "Patterns": "样币",
    "Tokens": "代币",
    "Medals": "纪念章",
    "Banknotes": "纸币",
    "Coins": "硬币",
    # —— 形状（shape）——
    "Round with hole": "圆形带孔",
    "Round": "圆形",
    "Circular": "圆形",
    "Scalloped": "花边形",
    "Rhombic": "菱形",
    "Rectangular": "长方形",
    "Square": "方形",
    "Polygonal": "多边形",
    "Octagonal": "八角形",
    "Hexagonal": "六角形",
    "Heptagonal": "七角形",
    "Wavy": "波浪形",
    "Fan-shaped": "扇形",
    "Other": "其他",
    # —— 材质（composition）——
    "Cupro-Nickel": "白铜",
    "Copper-Nickel": "白铜",
    "Nickel-Brass": "镍黄铜",
    "Aluminium-Bronze": "铝青铜",
    "Aluminum-Bronze": "铝青铜",
    "Manganese-Brass": "锰黄铜",
    "Sterling Silver": "斯特林银",
    "Bronze": "青铜",
    "Brass": "黄铜",
    "Copper": "铜",
    "Silver": "银",
    "Gold": "金",
    "Nickel": "镍",
    "Aluminium": "铝",
    "Aluminum": "铝",
    "Zinc": "锌",
    "Tin": "锡",
    "Iron": "铁",
    "Steel": "钢",
    "Lead": "铅",
    "Billon": "银铜合金",
    "Manganese": "锰",
    "Platinum": "铂金",
    "Palladium": "钯",
    "Titanium": "钛",
    # —— 币边（edge）——
    "Plain with incuse lettering": "光边带阴文",
    "Lettered": "铭文边",
    "Reeded": "齿边",
    "Milled": "齿边",
    "Grooved": "凹槽边",
    "Segmented": "分段边",
    "Decorated": "装饰边",
    "Security": "安全边",
    "Smooth": "光边",
    "Plain": "光边",
    # —— 国家/发行方（issuer）——
    "United Kingdom": "英国",
    "Great Britain": "英国",
    "Australia": "澳大利亚",
    "New Zealand": "新西兰",
    "Hong Kong": "香港",
    "Japan": "日本",
    "United States": "美国",
    "Canada": "加拿大",
    "India": "印度",
    "China": "中国",
    "Taiwan": "台湾",
    "Macau": "澳门",
    "South Korea": "韩国",
    "Singapore": "新加坡",
    "Malaysia": "马来西亚",
    "Thailand": "泰国",
    "Vietnam": "越南",
    "Philippines": "菲律宾",
    "Indonesia": "印度尼西亚",
    "France": "法国",
    "Germany": "德国",
    "Italy": "意大利",
    "Spain": "西班牙",
    "Portugal": "葡萄牙",
    "Netherlands": "荷兰",
    "Belgium": "比利时",
    "Switzerland": "瑞士",
    "Austria": "奥地利",
    "Sweden": "瑞典",
    "Norway": "挪威",
    "Denmark": "丹麦",
    "Finland": "芬兰",
    "Poland": "波兰",
    "Hungary": "匈牙利",
    "Greece": "希腊",
    "Turkey": "土耳其",
    "Russia": "俄罗斯",
    "Soviet Union": "苏联",
    "Ireland": "爱尔兰",
    "Mexico": "墨西哥",
    "Brazil": "巴西",
    "Argentina": "阿根廷",
    "Peru": "秘鲁",
    "Chile": "智利",
    "Panama": "巴拿马",
    "Colombia": "哥伦比亚",
    "South Africa": "南非",
    "Egypt": "埃及",
    "Israel": "以色列",
    "Iran": "伊朗",
    "Iraq": "伊拉克",
    "Saudi Arabia": "沙特阿拉伯",
    "Manchukuo": "满洲",
    "Straits Settlements": "海峡殖民地",
    "Guernsey": "根西",
    "Jersey": "泽西",
    "Belize": "伯利兹",
    "British Honduras": "英属洪都拉斯",
    "British West Africa": "英属西非",
    "East Africa": "东非",
    "Bulgaria": "保加利亚",
    "Cambodia": "柬埔寨",
    "Cyprus": "塞浦路斯",
    "Dominican Republic": "多米尼加共和国",
    "Eastern Caribbean States": "东加勒比国家",
    "French West Indies": "法属西印度群岛",
    "French Indochina": "法属印度支那",
    "Haiti": "海地",
    "British Palestine": "英属巴勒斯坦",
    "Liberia": "利比里亚",
    "Libya": "利比亚",
    "Luxembourg": "卢森堡",
    "Mauritius": "毛里求斯",
    "Monaco": "摩纳哥",
    "North Borneo": "北婆罗洲",
    "Sarawak": "砂拉越",
    "Netherlands Antilles": "荷属安的列斯",
    "Curaçao": "库拉索",
    "Sint Maarten": "圣马丁",
    "Nigeria": "尼日利亚",
    "Serbia": "塞尔维亚",
    "Seychelles": "塞舌尔",
    "Italian Somaliland": "意属索马里兰",
    "South African Republic": "南非共和国",
    "Zanzibar": "桑给巴尔",
    "Tanzania": "坦桑尼亚",
    "Uganda": "乌干达",
    "French Cochinchina": "法属交趾支那",
    "North Vietnam": "北越",
    # —— 执政者（ruler）——
    "Elizabeth II": "伊丽莎白二世",
    "George VI": "乔治六世",
    "George V": "乔治五世",
    "George IV": "乔治四世",
    "George III": "乔治三世",
    "George II": "乔治二世",
    "William IV": "威廉四世",
    "Edward VIII": "爱德华八世",
    "Edward VII": "爱德华七世",
    "Victoria": "维多利亚",
    "Napoleon III": "拿破仑三世",
    "Louis XIV": "路易十四",
    "Meiji": "明治",
    "Taishō": "大正",
    "Taisho": "大正",
    "Shōwa": "昭和",
    "Showa": "昭和",
    "Guangxu": "光绪",
    "Xuantong": "宣统",
    "Kangxi": "康熙",
    "Qianlong": "乾隆",
    # —— 面值单位（denomination / title）——
    "Penny": "便士",
    "Pence": "便士",
    "New Penny": "新便士",
    "Pound": "镑",
    "Shilling": "先令",
    "Farthing": "法新",
    "Florin": "弗罗林",
    "Groat": "格罗特",
    "Crown": "克朗",
    "Half": "半",
    "Quarter": "四分之一",
    "Cents": "分",
    "Cent": "分",
    "Centésimos": "分",
    "Centésimo": "分",
    "Doubles": "道布尔",
    "Double": "道布尔",
    "Öre": "欧尔",
    "Đồng": "盾",
    "Sant": "桑特",
    "Escudo": "埃斯库多",
    "Real": "雷亚尔",
    "Dollars": "元",
    "Dollar": "元",
    "Yen": "日元",
    "Yuan": "元",
    "Jiao": "角",
    "Fen": "分",
    "Franc": "法郎",
    "Peseta": "比塞塔",
    "Peso": "比索",
    "Rupee": "卢比",
    "Anna": "安那",
    "Paisa": "派士",
    "Mark": "马克",
    "Lira": "里拉",
    "Krone": "克朗",
    "Koruna": "克朗",
    "Gulden": "盾",
    "Rouble": "卢布",
    "Ruble": "卢布",
    "Baht": "铢",
    "Ringgit": "林吉特",
    "Sen": "钱",
    "Rin": "厘",
    "Mon": "文",
    "Duo": "双",
    "Cash": "文",
    "½": "半",
    "¼": "四分之一",
    "¾": "四分之三",
    # —— 造币厂（mints）——
    "Royal Mint": "皇家铸币厂",
    "Royal Canadian Mint": "加拿大皇家造币厂",
    "Tower Hill": "伦敦塔山",
    "Heaton and Sons": "希顿父子",
    "Heaton": "希顿",
    "Birmingham": "伯明翰",
    "Kolkata": "加尔各答",
    "Calcutta": "加尔各答",
    "Bombay": "孟买",
    "Ottawa": "渥太华",
    "Sydney": "悉尼",
    "Pretoria": "比勒陀利亚",
    "Osaka": "大阪",
    # —— 说明文本常用词（title / description / comment）——
    "3rd": "第三",
    "2nd": "第二",
    "1st": "第一",
    "4th": "第四",
    "portrait": "肖像",
    "bust": "半身像",
    "head": "头像",
    "facing left": "面向左",
    "facing right": "面向右",
    "Crowned": "戴冠",
    "diademed": "戴冠",
    "veiled": "戴头纱",
    "legend": "铭文",
    "around": "环绕",
    "Seated figure": "坐像",
    "Britannia": "不列颠尼亚",
    "denomination above": "面值在上",
    "date in exergue": "年份在下方",
    "withdrawn": "退役",
    "Proof": "精制",
    "Pattern": "样币",
    "Mule": "混配",
    "varieties": "版别",
    "variety": "版别",
    "Mintmarks": "厂标",
    "Mintmark": "厂标",
    "mint": "造币厂",
    "normal tide": "正常浪",
    "low tide": "低浪",
    "high tide": "高浪",
    "Dot after": "后带点",
    "Two date variants": "两种年份版",
    "see comments": "见备注",
    "Standard weight": "标准重量",
    "grains": "格令",
    "ounce": "盎司",
    "dies": "币模",
    "engraved by": "雕刻者",
    "design": "设计",
    "designer's initials": "设计师缩写",
    "below bust": "头像下方",
    "Note": "注",
    "issued": "发行",
    "struck": "铸造",
    "reverse": "背面",
    "obverse": "正面",
    "in three lines": "分三行",
    "within a circle of beads": "珠圈内",
    "scroll": "卷轴",
    "trident": "三叉戟",
    "shield": "盾",
    "Union flag": "联合旗",
    "sea behind": "背景为海",
}

_GLOSSARY_LONGEST_FIRST = tuple(
    sorted(GLOSSARY.items(), key=lambda item: len(item[0]), reverse=True)
)

# 预编译：整词命中（前后不挨字母），避免 Mint 咬进 Mints / Mintmark
_GLOSSARY_PATTERNS = tuple(
    (re.compile(rf"(?<![A-Za-z]){re.escape(en)}(?![A-Za-z])", re.IGNORECASE), zh)
    for en, zh in _GLOSSARY_LONGEST_FIRST
)


@lru_cache(maxsize=8192)
def to_zh(text: str | None) -> str | None:
    """按词表把英文替换成中文；未收录的片段保留英文原文（纯函数，结果可缓存）。"""
    if not text:
        return text
    result = text
    for pattern, zh in _GLOSSARY_PATTERNS:
        result = pattern.sub(zh, result)
    return result


def mints_to_zh(mints: list[str]) -> list[str]:
    return [to_zh(name) or name for name in mints]


# ---------------------------------------------------------------- HTML 片段 → 纯文本


# Numista 的 `comments` 是 HTML 片段（`<p>` / `<br>` / `<i>` / `<strong>`），而页面用
# `{{ series.comments }}`（Jinja 自动转义）渲染 —— 不剥掉就会把标签当**字面文本**显示出来。
_BLOCK_TAG = re.compile(r"<\s*/?\s*(?:br|p|div|li|ul|ol|tr|table)\b[^>]*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def strip_html(text: str | None) -> str | None:
    """HTML 片段 → 纯文本：块级 / 换行标签换成**空格**（避免前后词粘连），其余标签删掉，
    实体（`&amp;` 等）解码，最后把连续空白归一为单空格。"""
    if not text:
        return text
    result = _BLOCK_TAG.sub(" ", text)
    result = _ANY_TAG.sub("", result)
    result = html.unescape(result)
    return _WHITESPACE.sub(" ", result).strip() or None


# 可能带 HTML 的字段（系列 4 个 + 单版 1 个）；`strip_html_texts` 只动对象上存在的字段
HTML_TEXT_FIELDS = ("title", "obverse_desc", "reverse_desc", "comments", "comment")


def strip_html_texts(obj: T) -> T:
    """把「描述 / 备注 / 名称」类字段里的 HTML 标签剥成纯文本（系列与单版通用）。

    只用于**中文表**；英文表保留 Numista 原文（原始数据不动）。
    """
    changes = {
        field: strip_html(getattr(obj, field))
        for field in HTML_TEXT_FIELDS
        if hasattr(obj, field)
    }
    return replace(obj, **changes)


# 需要翻译的字段（与用户 2026-10-14 的清单一一对应）
SERIES_ZH_FIELDS = (
    "title",
    "object_type",
    "issuer",
    "ruler",
    "denomination",
    "shape",
    "composition",
    "edge",
    "obverse_desc",
    "reverse_desc",
    "comments",
)
ISSUE_ZH_FIELDS = ("mint", "comment", "edge")


def series_to_zh(series: CoinSeries) -> CoinSeries:
    """系列 → 中文版系列（只翻指定字段，其余原样）。"""
    changes = {field: to_zh(getattr(series, field)) for field in SERIES_ZH_FIELDS}
    changes["mints"] = mints_to_zh(series.mints)
    return replace(series, **changes)


def issue_to_zh(issue: CoinIssue) -> CoinIssue:
    """单版 → 中文版单版（只翻 mint / comment / edge）。"""
    return replace(issue, **{field: to_zh(getattr(issue, field)) for field in ISSUE_ZH_FIELDS})

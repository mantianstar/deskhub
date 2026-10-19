"""Numista 目录接口（铜币参考库）—— 系列 / 单版（spec §14.13 / plan §18.13）。

与 `gapi.py` 的分工：`gapi` 管**拍卖**（麦稀奇的专场/拍品/成交），本模块管**硬币本身**的
目录信息（numista.com 的系列与单版）。两者互不依赖。

站点事实（2026-10-14 实测，API v3，Key 走 `DESKHUB_NUMISTA_API_KEY`）：

- **搜索** `GET /types`：参数是 `q`（关键词）/ `issuer`（发行方 code）/ `year`（单年）/ `category`，
  **不是** `min_year` / `max_year`（实测这两个被忽略）。返回 `{"count": N, "types": [...]}`，
  每项是**轻量**系列卡（id / title / object_type / issuer / min_year / max_year / 缩略图）。
- **系列详情** `GET /types/{id}`：完整的系列字段（面值、尺寸、厚度、形状、材质、正反面描述/铭文/图片、KM 目录号…）。
- **单版** `GET /types/{id}/issues`：返回**裸数组**（不是对象），字段只有
  `id / year / gregorian_year / mintage / comment / mint_letter / references / marks`。
  —— **没有**造币厂名、版别备注、重量、厚度、币边、是否精制/样币、单版图片。
  故：尺寸/重量/厚度/币边用**系列层**的值兜底；厂标取 `mint_letter`；精制/样币按 `comment`
  关键词（proof / essai / pattern）标注；API 确实没有的一律留空，不臆造
  （与 spec §14.6「宁可空着，也不给不可验证的数字」同一精神）。
- 发行方 code 是**法文式 slug**，且「伞级」L1 会含殖民地（如 `united-kingdom` 含百慕大），
  本国是更靠下的那一级（英国=`royaume-uni`、澳大利亚=`australie`、日本=`japon`…）。
  中文输入靠下面的 `ISSUER_CODES` 映射到**本国** code。

- **目录号**：`references` 里**每一条**都取，写成 `目录代码#编号` 再逗号分隔
  （如 `KM#790,Sp#3961`）—— 不只取 KM（用户 2026-10-14 要求）。
- **中文版**：英文表（`coin_series` / `coin_issues`）之外，另有字段一致的 `*_cn` 表，
  由内置词表（`app/coins/translate.py`）把可翻译字段注成中文；其余字段原样复制。
- **风控**（用户 2026-10-14 要求）：免费额度每月 2000 次且**响应不带限流头**，只能自管 ——
  `request_interval_seconds` 拉大间隔、`max_requests_per_run` 限单轮、`monthly_limit` 限月度总额，
  每次请求落 `coin_api_usage` 计数。

设计取舍：解析是**纯函数**（只吃 dict），`NumistaClient` 只负责「发请求 → 交给纯函数」，
采集编排 `collect_series` / `collect_issues` 负责落库（`repository`），便于离线单测。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import AsyncIterator

import httpx

from app import llm, repository, textmatch
from app.config import Config
from app.coins import translate, translate_llm
from app.models import CoinIssue, CoinSeries

logger = logging.getLogger(__name__)

# 调用计数用的 provider 名（风控，见 CoinNumistaConfig）
PROVIDER = "numista"


class NumistaError(RuntimeError):
    """Numista 调用或返回内容异常（缺 Key / HTTP / 参数非法）。"""


# ---------------------------------------------------------------- 中文 → 英文

# 中文国家名 → Numista 发行方 code（**本国**那一级）。数值来自 2026-10-14 `/issuers` 实测。
# 未收录的国家：报错并提示怎么加，不猜（猜错的 code 会静默返回别国的币）。
ISSUER_CODES: dict[str, str] = {
    "英国": "royaume-uni",
    "英格兰": "royaume-uni",
    "澳大利亚": "australie",
    "澳洲": "australie",
    "新西兰": "nouvelle-zelande",
    "香港": "hong_kong",
    "日本": "japon",
    "美国": "etats-unis",
    "加拿大": "canada",
    "印度": "inde",
    "中国": "china",
    "台湾": "taiwan",
    "澳门": "macao",
    "韩国": "coree_du_sud",
    "新加坡": "singapour",
    "马来西亚": "malaisie",
    "泰国": "thailande",
    "越南": "viet-nam",
    "菲律宾": "philippines",
    "印度尼西亚": "indonesia",
    "印尼": "indonesia",
    "法国": "france",
    "德国": "germany",
    "意大利": "italie",
    "西班牙": "espagne",
    "葡萄牙": "portugal",
    "荷兰": "pays-bas",
    "比利时": "belgique",
    "瑞士": "switzerland",
    "奥地利": "autriche",
    "瑞典": "suede",
    "挪威": "norvege",
    "丹麦": "danemark",
    "芬兰": "finlande",
    "波兰": "pologne",
    "匈牙利": "hongrie",
    "希腊": "grece",
    "土耳其": "turquie",
    "俄罗斯": "russia",
    "苏联": "ancienne_urss",
    "爱尔兰": "irlande",
    "墨西哥": "mexique",
    "巴西": "bresil",
    "阿根廷": "argentine",
    "秘鲁": "perou",
    "智利": "chili",
    "巴拿马": "panama",
    "哥伦比亚": "colombie",
    "南非": "afrique_du_sud",
    "埃及": "egypte",
    "以色列": "israel",
    "伊朗": "iran",
    "伊拉克": "irak",
    "沙特阿拉伯": "arabie_saoudite",
    "满洲": "manchoukuo",
    "海峡殖民地": "straits",
    "马来亚": "malaya",
    "砂拉越": "sarawak",
    "捷克斯洛伐克": "tchecoslovaquie",
    "南斯拉夫": "yougoslavie",
}

# 面值单位（中文 → 英文）。取「最长优先」匹配，避免「便士」被「士」之类切碎。
DENOMINATION_UNITS: dict[str, str] = {
    "半便士": "half penny",
    "便士": "penny",
    "克朗": "crown",
    "先令": "shilling",
    "法新": "farthing",
    "芬尼": "penni",
    "派士": "paisa",
    "皮阿斯特": "piastre",
    "格罗申": "groschen",
    "捷克斯洛伐克": "koruna",
    "比塞塔": "peseta",
    "埃斯库多": "escudo",
    "第纳尔": "dinar",
    "里亚尔": "rial",
    "卢布": "rouble",
    "比索": "peso",
    "卢比": "rupee",
    "法郎": "franc",
    "里拉": "lira",
    "马克": "mark",
    "林吉特": "ringgit",
    "基尼": "guinea",
    "镑": "pound",
    "日元": "yen",
    "円": "yen",
    "元": "yuan",
    "圆": "yuan",
    "分": "cent",
    "钱": "sen",
    "毫": "mill",
    "厘": "cash",
    "文": "cash",
    "盾": "gulden",
    "铢": "baht",
    "苏": "sou",
}

# 中文数字（只处理逐字常见写法，够用即可；阿拉伯数字是主路径）
_CN_NUMERALS = {
    "零": "0", "一": "1", "两": "2", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
}

_ASCII_CODE = re.compile(r"^[a-z][a-z0-9_-]*$")
_LEADING_DIGITS = re.compile(r"^\s*(\d+)")
_UNITS_LONGEST_FIRST = tuple(sorted(DENOMINATION_UNITS, key=len, reverse=True))


def resolve_issuer(country: str) -> str:
    """中文国家名 → Numista 发行方 code。

    也接受**已是 code** 的输入（如 `royaume-uni`），方便绕过映射表直接指定。
    """
    text = (country or "").strip()
    if not text:
        raise NumistaError("国家不能为空")
    if text in ISSUER_CODES:
        return ISSUER_CODES[text]
    if _ASCII_CODE.match(text):
        return text  # 已经是 code，直接用
    raise NumistaError(
        f"不认识的国家 {text!r}；请在 app/coins/numista.py 的 ISSUER_CODES 里补一行，"
        "或直接传 Numista 发行方 code（如 royaume-uni）"
    )


def translate_denomination(denomination: str) -> str:
    """中文面值 → 英文关键词（如「1便士」→「1 penny」、「半便士」→「half penny」）。"""
    text = (denomination or "").strip()
    if not text:
        raise NumistaError("面值不能为空")

    # 「半 X」这类前缀词先处理
    if text.startswith("半"):
        unit = _unit_en(text[1:])
        return f"half {unit}" if unit else f"half {text[1:]}".strip()

    number, rest = _split_number(text)
    unit = _unit_en(rest) if rest else None
    if number is None:
        return unit or text  # 没有数字（如「英镑」）→ 直接给单位；认不出就把原文交给接口
    if unit is None:
        return f"{number} {rest}".strip() if rest else number
    return f"{number} {unit}"


def resolve_query(country: str, denomination: str) -> tuple[str, str]:
    """（国家, 面值）中文 → （发行方 code, 英文关键词）。"""
    return resolve_issuer(country), translate_denomination(denomination)


def _split_number(text: str) -> tuple[str | None, str]:
    match = _LEADING_DIGITS.match(text)
    if match:
        return match.group(1), text[match.end() :]
    if text and text[0] in _CN_NUMERALS:
        return _CN_NUMERALS[text[0]], text[1:]
    return None, text


def _unit_en(text: str) -> str | None:
    for unit in _UNITS_LONGEST_FIRST:
        if unit in text:
            return DENOMINATION_UNITS[unit]
    return None


# ---------------------------------------------------------------- 数据形状


@dataclass(frozen=True)
class SeriesInfo:
    """一个系列（Numista type）—— 对应「系列信息」那张清单。"""

    numista_id: int
    title: str | None
    url: str | None
    object_type: str | None
    issuer: str | None
    min_year: int | None
    max_year: int | None
    ruler: str | None
    denomination: str | None
    size_mm: float | None
    weight_g: float | None
    thickness_mm: float | None
    shape: str | None
    composition: str | None
    edge: str | None
    obverse_desc: str | None
    obverse_lettering: str | None
    obverse_picture: str | None
    reverse_desc: str | None
    reverse_lettering: str | None
    reverse_picture: str | None
    catalog_no: str | None
    comments: str | None
    mints: list[str]


@dataclass(frozen=True)
class IssueInfo:
    """一个单版（Numista issue）—— 对应「所有年份货币信息」那张清单。"""

    numista_id: int
    series_numista_id: int
    year: int | None
    gregorian_year: int | None
    mint: str | None
    mint_mark: str | None
    variety: str | None
    comment: str | None
    mintage: int | None
    size_mm: float | None
    weight_g: float | None
    thickness_mm: float | None
    edge: str | None
    is_proof: bool | None
    is_essai: bool | None
    picture: str | None
    catalog_no: str | None


# ---------------------------------------------------------------- 纯函数：解析


def _catalog_no(references: object) -> str | None:
    """从 references 抽**全部**目录号（`目录代码#编号`，逗号分隔），如 `KM#790,Sp#3961`。"""
    if not isinstance(references, list):
        return None
    parts: list[str] = []
    for ref in references:
        if not isinstance(ref, dict):
            continue
        code = (ref.get("catalogue") or {}).get("code")
        number = ref.get("number")
        if code and number:
            parts.append(f"{code}#{number}")
    return ",".join(parts) if parts else None


def _flag(comment: str | None, *keywords: str) -> bool | None:
    """按备注关键词推断布尔标记；没有备注 → None（未知，不猜）。"""
    if not comment:
        return None
    low = comment.lower()
    return any(keyword in low for keyword in keywords)


def series_from_type(data: dict) -> SeriesInfo:
    """`GET /types/{id}` 的返回 → `SeriesInfo`。"""
    ruler = ",".join(
        str(r["name"]) for r in data.get("ruler") or [] if isinstance(r, dict) and r.get("name")
    )
    obverse = data.get("obverse") or {}
    reverse = data.get("reverse") or {}
    return SeriesInfo(
        numista_id=int(data["id"]),
        title=data.get("title"),
        url=data.get("url"),
        object_type=(data.get("object_type") or {}).get("name"),
        issuer=(data.get("issuer") or {}).get("name"),
        min_year=data.get("min_year"),
        max_year=data.get("max_year"),
        ruler=ruler or None,
        denomination=(data.get("value") or {}).get("text"),
        size_mm=data.get("size"),
        weight_g=data.get("weight"),
        thickness_mm=data.get("thickness"),
        shape=data.get("shape"),
        composition=(data.get("composition") or {}).get("text"),
        edge=(data.get("edge") or {}).get("description"),
        obverse_desc=obverse.get("description"),
        obverse_lettering=obverse.get("lettering"),
        obverse_picture=obverse.get("picture"),
        reverse_desc=reverse.get("description"),
        reverse_lettering=reverse.get("lettering"),
        reverse_picture=reverse.get("picture"),
        catalog_no=_catalog_no(data.get("references")),
        comments=data.get("comments"),
        mints=[
            str(mint["name"])
            for mint in data.get("mints") or []
            if isinstance(mint, dict) and mint.get("name")
        ],
    )


def issue_from_issue(data: dict, series: SeriesInfo) -> IssueInfo:
    """`GET /types/{id}/issues` 的一条 → `IssueInfo`。

    单版接口只给 `year / mintage / comment / mint_letter / references`；
    尺寸/重量/厚度/币边**继承系列**，精制/样币按 `comment` 关键词标注，
    其余（造币厂名、版别备注、单版图片）接口没有 → 留空。
    """
    comment = data.get("comment")
    year = data.get("year")
    gregorian_year = data.get("gregorian_year")
    if year is None:
        year = gregorian_year
    return IssueInfo(
        numista_id=int(data["id"]),
        series_numista_id=series.numista_id,
        year=year,
        gregorian_year=gregorian_year,
        mint=None,
        mint_mark=data.get("mint_letter"),
        variety=None,
        comment=comment,
        mintage=data.get("mintage"),
        size_mm=series.size_mm,
        weight_g=series.weight_g,
        thickness_mm=series.thickness_mm,
        edge=series.edge,
        is_proof=_flag(comment, "proof"),
        is_essai=_flag(comment, "essai", "pattern"),
        picture=None,
        catalog_no=_catalog_no(data.get("references")),
    )


# ---------------------------------------------------------------- HTTP 客户端


class NumistaClient:
    """Numista v3 的薄封装；用法：`async with NumistaClient(cfg) as api:`。"""

    def __init__(self, config: Config, *, client: httpx.AsyncClient | None = None) -> None:
        self._cfg = config
        self._client = client
        self._owns_client = client is None
        self._last_call: float | None = None
        # 本轮已发出的请求数（单轮上限用；一个实例 = 一轮采集）
        self._calls_this_run = 0

    @property
    def _numista(self):
        return self._cfg.coins.numista

    @property
    def calls_this_run(self) -> int:
        return self._calls_this_run

    async def __aenter__(self) -> "NumistaClient":
        if self._client is None:
            if not self._numista.api_key:
                raise NumistaError(
                    f"未设置环境变量 {self._numista.api_key_env}，无法访问 Numista"
                    "（把 Key 写进 .env，见 .env.example）"
                )
            self._client = httpx.AsyncClient(
                base_url=self._numista.base_url.rstrip("/"),
                timeout=self._numista.timeout_seconds,
                headers={"Numista-API-Key": self._numista.api_key},
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _guard_quota(self) -> None:
        """风控：免费额度每月 2000 次且**响应不带限流头**，只能自管（用户 2026-10-14 要求）。

        - 单轮上限（`max_requests_per_run`）：一次 CLI 不无节制地刷；
        - 月度上限（`monthly_limit`）：按业务时区切月计数，用满就停，留余量给手工查。
        """
        cfg = self._numista
        if self._calls_this_run >= cfg.max_requests_per_run:
            raise NumistaError(
                f"本轮 Numista 请求已达上限 {cfg.max_requests_per_run} 次，先停下（下次再跑）"
            )
        used = repository.count_api_calls_since(
            PROVIDER, repository.local_month_start_utc(self._cfg.app.tz)
        )
        if used >= cfg.monthly_limit:
            raise NumistaError(
                f"本月 Numista 请求已达上限 {cfg.monthly_limit} 次（免费额度 2000），本月暂停调用"
            )
        if used + 1 >= cfg.monthly_limit * 0.9:
            logger.warning("Numista 本月请求已用 %d/%d，接近上限", used + 1, cfg.monthly_limit)

    async def _get(self, path: str, params: dict | None = None):
        if self._client is None:  # pragma: no cover - 只有误用才走到
            raise NumistaError("客户端尚未通过 async with 打开")
        self._guard_quota()
        # 限速：接口额度有限，请求之间留间隔（第一条不白等）
        interval = self._numista.request_interval_seconds
        if self._last_call is not None and interval > 0:
            await asyncio.sleep(interval)
        self._last_call = time.monotonic()
        try:
            response = await self._client.get(path, params=params or {})
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NumistaError(f"Numista {path} 调用失败：{exc}") from exc
        self._calls_this_run += 1
        repository.record_api_call(PROVIDER)
        return data

    async def search_type_ids(
        self,
        *,
        issuer: str | None = None,
        year: int | None = None,
        denomination: str | None = None,
    ) -> list[int]:
        """搜索系列号。

        实测参数是 `q` + `issuer` + `year`（`min_year`/`max_year` 会被忽略）。
        `issuer` / `year` 可省略：候选池扫描只按面值关键词全局搜。
        """
        params = {
            key: value
            for key, value in (
                ("q", denomination),
                ("issuer", issuer),
                ("year", year),
            )
            if value is not None
        }
        data = await self._get("/types", params)
        types = data.get("types") if isinstance(data, dict) else None
        return [
            int(item["id"])
            for item in types or []
            if isinstance(item, dict) and item.get("id") is not None
        ]

    async def type_detail(self, type_id: int) -> SeriesInfo:
        """系列详情（含面值 / 尺寸 / 材质 / 正反面 / KM 目录号…）。"""
        return series_from_type(await self._get(f"/types/{type_id}"))

    async def issues(self, type_id: int, *, series: SeriesInfo) -> list[IssueInfo]:
        """系列下全部单版（`/types/{id}/issues` 返回**裸数组**）。"""
        data = await self._get(f"/types/{type_id}/issues")
        if not isinstance(data, list):
            return []
        return [
            issue_from_issue(item, series)
            for item in data
            if isinstance(item, dict) and item.get("id") is not None
        ]


# ---------------------------------------------------------------- 采集编排（落库）


def _series_row(info: SeriesInfo, fetched_at: str) -> CoinSeries:
    return CoinSeries(
        id=None,
        numista_id=info.numista_id,
        title=info.title,
        url=info.url,
        object_type=info.object_type,
        issuer=info.issuer,
        min_year=info.min_year,
        max_year=info.max_year,
        ruler=info.ruler,
        denomination=info.denomination,
        size_mm=info.size_mm,
        weight_g=info.weight_g,
        thickness_mm=info.thickness_mm,
        shape=info.shape,
        composition=info.composition,
        edge=info.edge,
        obverse_desc=info.obverse_desc,
        obverse_lettering=info.obverse_lettering,
        obverse_picture=info.obverse_picture,
        reverse_desc=info.reverse_desc,
        reverse_lettering=info.reverse_lettering,
        reverse_picture=info.reverse_picture,
        catalog_no=info.catalog_no,
        comments=info.comments,
        mints=info.mints,
        fetched_at=fetched_at,
    )


def _issue_row(issue: IssueInfo, series_id: int, fetched_at: str) -> CoinIssue:
    return CoinIssue(
        id=None,
        numista_id=issue.numista_id,
        series_id=series_id,
        year=issue.year,
        gregorian_year=issue.gregorian_year,
        mint=issue.mint,
        mint_mark=issue.mint_mark,
        variety=issue.variety,
        comment=issue.comment,
        mintage=issue.mintage,
        size_mm=issue.size_mm,
        weight_g=issue.weight_g,
        thickness_mm=issue.thickness_mm,
        edge=issue.edge,
        is_proof=issue.is_proof,
        is_essai=issue.is_essai,
        picture=issue.picture,
        catalog_no=issue.catalog_no,
        fetched_at=fetched_at,
    )


@asynccontextmanager
async def _translator(
    config: Config, llm_client: llm.LLMClient | None
) -> AsyncIterator[llm.LLMClient | None]:
    """拿一个可用的大模型客户端做「描述 / 备注 / 名称」翻译；没配 Key 就给 None（回退词表）。

    外部注入的客户端不在这里关闭（归调用方管）；自己建的用完关掉。
    """
    if llm_client is not None:
        yield llm_client
        return
    try:
        client = llm.get_client(config)
    except llm.LLMError as exc:
        logger.info("未配置大模型，铜币描述/备注/名称翻译回退内置词表：%s", exc)
        yield None
        return
    try:
        yield client
    finally:
        await client.aclose()


async def _cn_series_info(
    info: SeriesInfo, llm_client: llm.LLMClient | None
) -> SeriesInfo:
    """系列 → 中文版：枚举字段走词表；名称 / 描述 / 备注交大模型，失败逐项回退词表。

    最后统一把描述 / 备注 / 名称里的 HTML 标签剥掉（Numista 的 `comments` 是 HTML 片段，
    页面按自动转义渲染，留着标签会当字面文本显示）。
    """
    cn = translate.series_to_zh(info)
    if llm_client is not None:
        try:
            texts = await translate_llm.series_texts_zh(llm_client, info)
        except llm.LLMError as exc:
            logger.warning("系列 %s 描述/备注大模型翻译失败，回退内置词表：%s", info.numista_id, exc)
        else:
            # 国家 / 执政者 / 面值优先用词表：词表已译出中文时忽略大模型（避免「8 Doubles → 8 双倍」这类过度意译）
            for field in ("issuer", "ruler", "denomination"):
                if getattr(cn, field) != getattr(info, field):
                    texts.pop(field, None)
            cn = replace(cn, **texts)
    return translate.strip_html_texts(cn)


async def _cn_issue_infos(
    issues: list[IssueInfo], llm_client: llm.LLMClient | None
) -> list[IssueInfo]:
    """单版清单 → 中文清单：mint / edge 走词表；备注整批交大模型，失败回退词表（再剥 HTML）。"""
    cn = [translate.issue_to_zh(issue) for issue in issues]
    if llm_client is not None:
        try:
            mapping = await translate_llm.issue_texts_zh(
                llm_client, [(issue.numista_id, issue.comment or "") for issue in issues]
            )
        except llm.LLMError as exc:
            logger.warning("本批单版备注大模型翻译失败，回退内置词表：%s", exc)
        else:
            cn = [
                replace(base, comment=mapping.get(issue.numista_id) or base.comment)
                for base, issue in zip(cn, issues)
            ]
    return [translate.strip_html_texts(issue) for issue in cn]


async def _store_series(
    info: SeriesInfo, fetched_at: str, *, llm_client: llm.LLMClient | None
) -> None:
    """英文表 + 中文表各写一份（中文表的描述 / 备注 / 名称由大模型翻，见 `translate_llm`）。"""
    repository.upsert_coin_series(_series_row(info, fetched_at))
    cn_info = await _cn_series_info(info, llm_client)
    repository.upsert_coin_series_cn(_series_row(cn_info, fetched_at))


async def _store_issues(
    issues: list[IssueInfo],
    db_id: int,
    cn_id: int,
    fetched_at: str,
    *,
    llm_client: llm.LLMClient | None,
) -> None:
    for issue in issues:
        repository.upsert_coin_issue(_issue_row(issue, db_id, fetched_at))
    for cn_issue in await _cn_issue_infos(issues, llm_client):
        repository.upsert_coin_issue_cn(_issue_row(cn_issue, cn_id, fetched_at))


def _denomination_hit(info: SeriesInfo, keyword: str, raw_denomination: str) -> bool:
    """面值是否匹配：英文关键词 vs 英文 `value.text`，或用户原文 vs 中文 `value.text`。

    Numista 的 `q` 是**模糊搜索** —— 查 `1 penny` 会连 `½ Penny` 一起返回（2026-10-15 实测），
    且 `/types` **没有**面值筛选参数，所以取到详情后必须自己再挡一道。
    英文比一次（关键词可能已是英文），中文再比一次（`½ Penny` →「半 便士」，用户的「半便士」才对得上）。
    """
    return textmatch.contains(info.denomination, keyword) or textmatch.contains(
        translate.to_zh(info.denomination), raw_denomination
    )


def _composition_hit(info: SeriesInfo, raw_composition: str) -> bool:
    """材质是否匹配：用户输入 vs 英文 `composition.text`，或 vs 中文（用户多半写中文）。"""
    return textmatch.contains(info.composition, raw_composition) or textmatch.contains(
        translate.to_zh(info.composition), raw_composition
    )


def _series_matches(
    info: SeriesInfo, *, keyword: str, raw_denomination: str, raw_composition: str
) -> bool:
    """取到详情后的**精确过滤**：面值必筛，材质非空才筛（spec §14.14）。"""
    if not _denomination_hit(info, keyword, raw_denomination):
        return False
    if raw_composition and not _composition_hit(info, raw_composition):
        return False
    return True


async def collect_series(
    config: Config,
    *,
    country: str,
    year: int,
    denomination: str,
    composition: str = "",
    client: NumistaClient | None = None,
    limit: int | None = None,
    llm_client: llm.LLMClient | None = None,
) -> list[SeriesInfo]:
    """按「国家 + 年份 + 面值」搜系列、取详情，**只落库面值 / 材质匹配的**。返回入库清单。

    为什么是「取详情后过滤」：`/types` 的 `q` 只做模糊匹配，且**没有**面值 / 材质筛选参数
    （2026-10-15 实测：`denomination` / `value` / `currency` 传了都被静默忽略；只有 `material=<id>`
    能按材质筛，但要维护材质 id 映射）。所以先按 id 取详情，再用 `value.text` / `composition.text`
    比对 —— 不匹配的**不落库**（用户 2026-10-15：「多余的系列对我来说没有用」）。

    中文表的描述 / 备注 / 名称由大模型翻（`llm_client` 注入给测试；不传则按配置取，
    没配 Key 就回退内置词表，见 `translate_llm`）。
    """
    issuer, keyword = resolve_query(country, denomination)
    cap = limit if limit is not None else config.coins.numista.max_series

    owned = client is None
    api = client or NumistaClient(config)
    if owned:
        await api.__aenter__()
    try:
        async with _translator(config, llm_client) as translator:
            type_ids = await api.search_type_ids(issuer=issuer, year=year, denomination=keyword)
            picked = type_ids[:cap]
            fetched_at = repository.utcnow()
            series_list: list[SeriesInfo] = []
            skipped = 0
            for type_id in picked:
                info = await api.type_detail(type_id)
                if not _series_matches(
                    info,
                    keyword=keyword,
                    raw_denomination=denomination,
                    raw_composition=composition,
                ):
                    skipped += 1
                    logger.info(
                        "Numista 过滤：跳过不匹配的系列 %s（面值=%s 材质=%s）",
                        info.numista_id,
                        info.denomination,
                        info.composition,
                    )
                    continue
                await _store_series(info, fetched_at, llm_client=translator)
                series_list.append(info)
            logger.info(
                "Numista 系列：%s %s %s → 命中 %d 个，入库 %d 个（过滤 %d 个）",
                country,
                year,
                keyword,
                len(type_ids),
                len(series_list),
                skipped,
            )
            return series_list
    finally:
        if owned:
            await api.__aexit__(None, None, None)


async def collect_issues(
    config: Config,
    *,
    series_id: int,
    client: NumistaClient | None = None,
    llm_client: llm.LLMClient | None = None,
) -> tuple[SeriesInfo, list[IssueInfo]]:
    """按系列号取系列详情 + 全部单版并落库。返回（系列, 单版清单）。

    中文表的描述 / 备注 / 名称由大模型翻（同 `collect_series`，失败回退内置词表）。
    """
    owned = client is None
    api = client or NumistaClient(config)
    if owned:
        await api.__aenter__()
    try:
        async with _translator(config, llm_client) as translator:
            info = await api.type_detail(series_id)
            fetched_at = repository.utcnow()
            db_id = repository.upsert_coin_series(_series_row(info, fetched_at))
            cn_info = await _cn_series_info(info, translator)
            cn_id = repository.upsert_coin_series_cn(_series_row(cn_info, fetched_at))
            issues = await api.issues(series_id, series=info)
            await _store_issues(issues, db_id, cn_id, fetched_at, llm_client=translator)
            logger.info("Numista 单版：系列 %s（%s）入库 %d 条", series_id, info.title, len(issues))
            return info, issues
    finally:
        if owned:
            await api.__aexit__(None, None, None)

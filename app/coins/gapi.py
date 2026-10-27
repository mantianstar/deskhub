"""麦稀奇小程序网关（gapi）—— 采集与查价的**唯一**站点来源（spec §14.3 / plan §18.3）。

站点事实（2026-10-12 实测）：
- `gapi.mxiqi.com` 是小程序/App 用的网关，**免登录、免签名**，普通 JSON POST 就能取数据，
  不走 `www.mxiqi.com` 那套滑块风控 —— 所以整个模块不再需要浏览器。
- 三个接口就够：
  - `miniprogram.auction.ListAuction`，body `{"auction_mode": 1|2}`（1=实时专场，2=限时专场）
    → 当前专场列表（含 `start_time` / **`end_time`（=截拍时间）** / `num` / `status` / 店铺 / 围观）；
  - `miniprogram.auction.get`，body `{"auction_id": <id>}`
    → 专场详情 + 店铺 + **全部拍品**（`auction_items.items`，一次给全，也支持翻页）；
  - `miniprogram.home.filter`，body `{"search_params": {"keywords": <词>, "status": 2}}`
    → **站内历史成交**（`status=2` 即成交，逐条带 `price`/`name`/`end_time`）—— 推荐价的证据来源。
- 价格字段单位是**分**（`price_deposit=2000` 即 ¥20）；图片是 OSS 的 key，要拼 `cdn.oss` 前缀。
- 场次状态：`status` 0=预展 / 1=进行中 / 2=公示。

设计取舍：解析是**纯函数**（只吃 dict），`MxiqiGapi` 只负责「发请求 → 交给纯函数」，便于离线单测。
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.config import Config
from app.coins.graded import is_graded_title
from app.coins.pricing import SoldSample

logger = logging.getLogger(__name__)

# 本模块用到的三个网关动作
ACTION_LIST = "miniprogram.auction.ListAuction"
ACTION_GET = "miniprogram.auction.get"
ACTION_FILTER = "miniprogram.home.filter"
# 分类字典：`data.cates[]` 一级、`sub[]` 二级（2026-10-27 实测可用）
ACTION_CATE = "miniprogram.cate.list"

# 专场状态码 → 我们的三态（spec §14.7）
SESSION_STATUS_BY_CODE = {0: "preview", 1: "running", 2: "closed"}
# 专场类型：站内 `auction_mode` 1=实时 / 2=限时
SESSION_KIND_BY_MODE = {1: "live", 2: "timed"}
KIND_LIVE = "live"
# 站内「成交」的 status 取值（同网页 `/auction/item?…&status=成交`）
SOLD_STATUS = 2
# 拍品终态：item.status 2=成交 / 3=未成交；1=在拍（不落终态，见 plan/coins.md §18.18）
LOT_OUTCOME_BY_CODE = {2: "sold", 3: "unsold"}
OUTCOME_SOLD = "sold"

# 图片 key 的默认前缀；`get` 会在 `cdn.oss` 里给准确值，列表页没有就退回这个
DEFAULT_OSS_BASE = "https://oss.mxiqi.com/"

# 一次请求取回专场详情的超时（网关很快，给宽一点即可）
REQUEST_TIMEOUT_SECONDS = 20

# 网关限频：429 时退避重试，别让整轮采集直接失败（风控优先于速度）
RATE_LIMIT_STATUS = 429
RETRY_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 2.0

_WEEKDAYS = "一二三四五六日"


class GapiError(RuntimeError):
    """网关调用或返回内容异常（HTTP / 业务错误码）。"""


# ---------------------------------------------------------------- 数据形状


@dataclass(frozen=True)
class ShopInfo:
    mxiqi_uid: str | None
    name: str | None
    avatar_url: str | None
    badges: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class CategoryInfo:
    """`miniprogram.cate.list` 的一个分类节点（一级 / 二级）。"""

    code: str
    name: str
    parent_code: str | None
    level: int
    thumb: str | None = None
    # 站点 `count`：随站点变化的快照，不是实时值
    item_count: int | None = None
    order_no: int | None = None


@dataclass(frozen=True)
class SessionCard:
    mxiqi_id: str
    title: str
    status: str | None
    # 站内文案形如「10月12日 星期一 19:30」；实时专场取**开拍时间**（start_time），
    # 限时专场取**截拍时间**（end_time）
    auction_at_raw: str | None
    lot_total: int | None
    view_count: int | None
    cover_urls: list[str]
    shop: ShopInfo | None = None
    # 截拍时间的 UTC ISO8601（限时专场按「当日截拍」采集，见 spec §14.17）
    end_at: str | None = None
    # live=实时专场 / timed=限时专场
    kind: str | None = None
    # 开拍时间的 UTC ISO8601（实时专场按「今日开拍」采集）
    start_at: str | None = None
    # 列表卡只嵌 5 条拍品预览；只有这里带 bid_num / bid_user_num（plan §18.18.1 P1-6）
    preview_lots: tuple["LotInfo", ...] = ()


@dataclass(frozen=True)
class LotInfo:
    mxiqi_id: str
    lot_no: str | None
    title: str
    start_price: float | None
    current_price: float | None
    image_urls: list[str]
    tags: dict[str, str]
    # 终态：在拍 / 未回采时全为 None（plan/coins.md §18.18）
    outcome: str | None = None
    final_price: float | None = None
    winner_uid: str | None = None
    closed_at: str | None = None
    # 热度：ListAuction 预览 / home.filter 有，auction.get 缺键即 None
    bid_num: int | None = None
    bid_user_num: int | None = None
    bid_source: str | None = None
    # 同图识别的 OSS key（去 base、去 query；plan §18.18.1 P1-7）
    image_keys: list[str] = field(default_factory=list)
    # 站点分类：取最细一级（`cate_id_2` 优先、回落 `cate_id_1`；plan §18.19.4）
    category_code: str | None = None
    # 评级盒：标题命中评级机构 / 「评级|盒」即 1，裸币 0（plan §18.19.6）
    is_graded: int = 0


@dataclass(frozen=True)
class SessionDetail:
    card: SessionCard
    deposit: float | None
    lots: list[LotInfo]
    # 截拍时间的 UTC ISO8601（站内是北京时间；落库口径统一用 UTC）
    end_at: str | None = None
    # 费率与运费（plan §18.18.1 P1-8）；详情拿不到时为 None
    commission_rate: float | None = None
    commission_min: float | None = None
    express_desc: str | None = None
    express_day: int | None = None
    express_fee: float | None = None
    express_free_amount: float | None = None
    is_storage: bool = False


# ---------------------------------------------------------------- 纯函数：解析


def _money(value: object) -> float | None:
    """"分" → "元"。站点价格一律是整数分。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value) / 100, 2)
    except (TypeError, ValueError):
        return None


def _number(value: object) -> float | None:
    """原样数值（费率 / 天数这类不按「分」换算的字段）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _whole(value: object) -> int | None:
    """整数计数（`bid_num` / `bid_user_num`），缺键或脏值一律 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def category_code_from_item(item: dict) -> str | None:
    """item → 站点分类码：`cate_id_2` 优先，缺失 / 0 回落 `cate_id_1`，都没有记 None。

    只存**最细一级**（plan/coins.md §18.19.3）：老数据（2019–2021）这两个键可能是 0，
    不能当成分类写入；`cate_info`（名称）覆盖率仅 1%，不作为数据源。
    """
    for key in ("cate_id_2", "cate_id_1"):
        code = _whole(item.get(key))
        if code is not None and code > 0:
            return str(code)
    return None


def category_from_cate(item: dict, *, parent_code: str | None = None) -> CategoryInfo | None:
    """`cates[]` 节点 → `CategoryInfo`；码 / 名称缺失视为脏数据丢弃。

    `parent_code` 显式传入时以它为准（二级节点在 `sub[]` 里，本身可能不带 `parent_id`）。
    """
    code = _whole(item.get("cate_id"))
    name = (item.get("name") or "").strip()
    if code is None or code <= 0 or not name:
        return None
    parent = parent_code
    if parent is None:
        parent_id = _whole(item.get("parent_id"))
        parent = str(parent_id) if parent_id is not None and parent_id > 0 else None
    level = _whole(item.get("level"))
    if level is None or level <= 0:
        level = 2 if parent else 1
    thumb = (item.get("thumb") or "").strip() or None
    return CategoryInfo(
        code=str(code),
        name=name,
        parent_code=parent,
        level=level,
        thumb=thumb,
        item_count=_whole(item.get("count")),
        order_no=_whole(item.get("order_no")),
    )


_TAG_RE = re.compile(r"<[^>]+>")
_EXPRESS_FEE_RE = re.compile(r"运费\s*[¥￥]?\s*(\d+(?:\.\d+)?)")
_EXPRESS_FREE_RE = re.compile(r"满\s*([0-9]+(?:\.[0-9]+)?|[百千万])\s*(?:元)?\s*包邮")
_FREE_UNITS = {"百": 100.0, "千": 1000.0, "万": 10000.0}


def _plain_text(raw: str | None) -> str:
    """富文本 → 纯文本：只去标签、反转义，不做结构化解析。"""
    if not raw:
        return ""
    return html.unescape(_TAG_RE.sub(" ", raw))


def parse_express_fee(desc: str | None) -> float | None:
    """运费说明 → 基础运费（元）：取首个「运费 ¥N」，**忽略地区差异**。"""
    match = _EXPRESS_FEE_RE.search(_plain_text(desc))
    return float(match.group(1)) if match else None


def parse_express_free_amount(text: str | None) -> float | None:
    """「满百包邮 / 满 200 元包邮」→ 包邮门槛（元）。"""
    match = _EXPRESS_FREE_RE.search(_plain_text(text))
    if match is None:
        return None
    raw = match.group(1)
    return _FREE_UNITS.get(raw) or float(raw)


def parse_is_storage(text: str | None) -> bool:
    """说明里出现「寄存」即视为可寄存（拍下先存店、凑单再发）。"""
    return "寄存" in _plain_text(text)


def _image_url(base: str, key: str | None) -> str | None:
    if not key:
        return None
    if key.startswith("http://") or key.startswith("https://"):
        return key
    return base.rstrip("/") + "/" + key.lstrip("/")


def _images(base: str, thumbs: list | None, limit: int) -> list[str]:
    urls: list[str] = []
    for thumb in thumbs or []:
        if not isinstance(thumb, dict):
            continue
        url = _image_url(base, thumb.get("thumb"))
        if url and url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _oss_key(raw: object) -> str | None:
    """缩略图地址 → OSS key：去 base（http 取 path）、去 query / fragment。"""
    if not isinstance(raw, str):
        return None
    key = raw.split("#", 1)[0].split("?", 1)[0].strip()
    if "://" in key:
        key = urlsplit(key).path.lstrip("/")
    return key or None


def _oss_keys(thumbs: list | None, limit: int) -> list[str]:
    keys: list[str] = []
    for thumb in thumbs or []:
        if not isinstance(thumb, dict):
            continue
        key = _oss_key(thumb.get("thumb"))
        if key and key not in keys:
            keys.append(key)
        if len(keys) >= limit:
            break
    return keys


def format_end_at(raw: str | None) -> str | None:
    """把站点的 `2026-10-12 19:30:00` 写成页面上的「10月12日 星期一 19:30」。"""
    parsed = parse_site_time(raw)
    if parsed is None:
        return None
    return f"{parsed.month}月{parsed.day}日 星期{_WEEKDAYS[parsed.weekday()]} {parsed:%H:%M}"


def parse_site_time(raw: str | None) -> datetime | None:
    """解析站点的 `YYYY-MM-DD HH:MM:SS`（**朴素北京时间**，无时区后缀）。"""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def to_utc_iso(raw: str | None, tz) -> str | None:
    """站点时间（业务时区）→ UTC ISO8601。"""
    parsed = parse_site_time(raw)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=tz).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def shop_from(org: dict | None) -> ShopInfo | None:
    """店铺：`owner_id` 就是站内 `/user.main.entry/{uid}` 的那个 uid。"""
    if not org:
        return None
    uid = org.get("owner_id") or org.get("oid")
    name = org.get("name") or org.get("owner_name")
    if uid is None and not name:
        return None

    verified = org.get("status_verify", org.get("owner_verified_status"))
    badges: list[dict] = []
    if verified == 1:
        badges.append({"kind": "认证", "label": "认证"})
    if (org.get("deposit") or 0) > 0:
        badges.append({"kind": "保证金", "label": "保证金"})
    years = org.get("register_years")
    if years:
        badges.append({"kind": "years", "label": f"{int(years)}年店铺"})
    return ShopInfo(
        mxiqi_uid=str(uid) if uid is not None else None,
        name=name,
        avatar_url=org.get("thumb") or org.get("owner_thumb"),
        badges=badges,
    )


def _preview_items(item: dict) -> list[dict]:
    """列表卡里嵌的拍品预览（只有 5 条，不是全量）。"""
    block = item.get("auction_items") or {}
    items = block.get("items") if isinstance(block, dict) else None
    return [raw for raw in (items or []) if isinstance(raw, dict)]


def card_from_auction(
    item: dict, *, oss_base: str = DEFAULT_OSS_BASE, tz=None, mode: int | None = None
) -> SessionCard | None:
    """`ListAuction` 的一张专场卡 → `SessionCard`。

    `mode` 是请求用的 `auction_mode`；缺省看条目自带的 `auction_mode`。
    """
    auction_id = item.get("auction_id")
    if auction_id is None:
        return None
    kind = SESSION_KIND_BY_MODE.get(mode if mode is not None else item.get("auction_mode"))
    at_raw = item.get("start_time") if kind == KIND_LIVE else item.get("end_time")
    preview_lots = tuple(
        lot
        for lot in (
            lot_from_item(raw, oss_base=oss_base, bid_source="preview")
            for raw in _preview_items(item)
        )
        if lot is not None
    )
    return SessionCard(
        mxiqi_id=str(auction_id),
        title=(item.get("name") or "").strip() or f"专场 {auction_id}",
        status=SESSION_STATUS_BY_CODE.get(item.get("status")),
        auction_at_raw=format_end_at(at_raw),
        lot_total=item.get("num"),
        view_count=(item.get("stat") or {}).get("pv"),
        cover_urls=_images(oss_base, item.get("thumbs"), 4),
        shop=shop_from(item.get("org_info")),
        end_at=to_utc_iso(item.get("end_time"), tz) if tz is not None else None,
        kind=kind,
        start_at=to_utc_iso(item.get("start_time"), tz) if tz is not None else None,
        preview_lots=preview_lots,
    )


def lot_from_item(
    item: dict,
    *,
    oss_base: str = DEFAULT_OSS_BASE,
    tz: ZoneInfo | None = None,
    bid_source: str | None = None,
) -> LotInfo | None:
    """`auction_items.items` 的一条拍品 → `LotInfo`。

    `status` 2/3 时落终态；`closed_at` 取 `end_time`（已结束场次等于场次截拍时刻，
    见 spec/coins.md §14.18.8），需要 `tz` 才能转 UTC，未传则留空。
    `bid_num` / `bid_user_num` 只在 `ListAuction` 预览与 `home.filter` 里有，
    `auction.get` 缺键时保持 NULL（`bid_source` 也留空）。
    """
    item_id = item.get("item_id")
    title = (item.get("name") or "").strip()
    if item_id is None or not title:
        return None
    order_no = item.get("order_no")
    tags = {
        str(attr.get("attr_name")): str(attr.get("attr_value"))
        for attr in (item.get("attrs") or [])
        if isinstance(attr, dict) and attr.get("attr_name") and attr.get("attr_value")
    }
    outcome = LOT_OUTCOME_BY_CODE.get(item.get("status"))  # type: ignore[arg-type]
    winner_uid = item.get("won_user_id")
    bid_num = _whole(item.get("bid_num"))
    bid_user_num = _whole(item.get("bid_user_num"))
    return LotInfo(
        mxiqi_id=str(item_id),
        lot_no=f"Lot.{order_no}" if order_no else None,
        title=title,
        start_price=_money(item.get("price_start")),
        current_price=_money(item.get("price")),
        image_urls=_images(oss_base, item.get("thumbs"), 3),
        tags=tags,
        outcome=outcome,
        # 未成交条目站点不给 price，成交价只对成交条目有意义
        final_price=_money(item.get("price")) if outcome == OUTCOME_SOLD else None,
        winner_uid=str(winner_uid) if winner_uid is not None else None,
        closed_at=to_utc_iso(item.get("end_time"), tz) if outcome and tz else None,
        bid_num=bid_num,
        bid_user_num=bid_user_num,
        # 两个键都没有时不算任何来源给过值，保持 NULL
        bid_source=bid_source if bid_num is not None or bid_user_num is not None else None,
        image_keys=_oss_keys(item.get("thumbs"), 3),
        category_code=category_code_from_item(item),
        is_graded=is_graded_title(title),
    )


def detail_from_payload(
    data: dict, fallback: SessionCard | None = None, tz: ZoneInfo | None = None
) -> SessionDetail:
    """`miniprogram.auction.get` 的 data → `SessionDetail`。

    专场卡拿不到的字段用 `fallback`（列表页那张卡）兜底。
    """
    auction = data.get("auction") or {}
    org = data.get("org_info") or {}
    oss_base = (data.get("cdn") or {}).get("oss") or DEFAULT_OSS_BASE

    auction_id = auction.get("auction_id")
    mxiqi_id = str(auction_id) if auction_id is not None else (fallback.mxiqi_id if fallback else "")
    lots_block = data.get("auction_items") or {}
    items = lots_block.get("items") or []
    lots = [
        lot
        for lot in (lot_from_item(item, oss_base=oss_base, tz=tz) for item in items)
        if lot
    ]

    cover_urls = _images(oss_base, (items[0].get("thumbs") if items else None), 4) or (
        list(fallback.cover_urls) if fallback else []
    )
    kind = SESSION_KIND_BY_MODE.get(auction.get("auction_mode")) or (
        fallback.kind if fallback else None
    )
    at_raw = auction.get("start_time") if kind == KIND_LIVE else auction.get("end_time")
    # 运费 / 包邮 / 寄存只出现在富文本里：原样存 express_desc，另抽基础值落数值列
    express_desc = auction.get("express_desc")
    policy_text = f"{_plain_text(express_desc)} {_plain_text(auction.get('desc_orginal'))}"
    express_day = _number(auction.get("express_day"))
    card = SessionCard(
        mxiqi_id=mxiqi_id,
        title=(auction.get("name") or (fallback.title if fallback else "") or f"专场 {mxiqi_id}").strip(),
        status=SESSION_STATUS_BY_CODE.get(auction.get("status"))
        if auction.get("status") is not None
        else (fallback.status if fallback else None),
        auction_at_raw=format_end_at(at_raw)
        or (fallback.auction_at_raw if fallback else None),
        lot_total=auction.get("num") or (fallback.lot_total if fallback else None),
        view_count=(data.get("auction_stat") or {}).get("pv")
        or (fallback.view_count if fallback else None),
        cover_urls=cover_urls,
        shop=shop_from(org) or (fallback.shop if fallback else None),
        kind=kind,
    )
    return SessionDetail(
        card=card,
        deposit=_money(auction.get("price_deposit")),
        lots=lots,
        commission_rate=_number(auction.get("commission_rate")),
        commission_min=_number(auction.get("commission_min")),
        express_desc=express_desc,
        express_day=int(express_day) if express_day is not None else None,
        express_fee=parse_express_fee(express_desc),
        express_free_amount=parse_express_free_amount(policy_text),
        is_storage=parse_is_storage(policy_text),
    )


def sold_from_payload(data: dict, tz) -> list[SoldSample]:
    """`miniprogram.home.filter` 的 data → 成交样本（title / price / sold_at / item_id）。

    只取 `status == SOLD_STATUS`（成交）的条目：`price` 是成交价（分），
    `end_time` 是这条拍品的成交时间（当 `sold_at` 用，供 180 天窗口过滤）；
    `item_id` 是成交明细的去重键（plan/coins.md §18.18 P0-3）。
    """
    items = ((data.get("auction_items") or {}).get("items")) or []
    samples: list[SoldSample] = []
    for item in items:
        if item.get("status") != SOLD_STATUS:
            continue
        title = (item.get("name") or "").strip()
        price = _money(item.get("price"))
        if not title or price is None:
            continue
        item_id = item.get("item_id")
        samples.append(
            SoldSample(
                title=title,
                price=price,
                sold_at=to_utc_iso(item.get("end_time"), tz),
                item_id=str(item_id) if item_id is not None else None,
                bid_num=_whole(item.get("bid_num")),
                bid_user_num=_whole(item.get("bid_user_num")),
                category_code=category_code_from_item(item),
            )
        )
    return samples


# ---------------------------------------------------------------- HTTP 客户端


class MxiqiGapi:
    """gapi 的薄封装；用法：`async with MxiqiGapi(cfg) as api:`。"""

    def __init__(self, config: Config, *, client: httpx.AsyncClient | None = None) -> None:
        self._cfg = config
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "MxiqiGapi":
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._cfg.coins.gapi_base_url.rstrip("/"),
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"Content-Type": "application/json"},
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _call(self, action: str, payload: dict) -> dict:
        if self._client is None:  # pragma: no cover - 只有误用才走到
            raise GapiError("客户端尚未通过 async with 打开")
        body: dict = {}
        for attempt in range(RETRY_ATTEMPTS + 1):
            try:
                response = await self._client.post(f"/{action}", json=payload)
                if response.status_code == RATE_LIMIT_STATUS and attempt < RETRY_ATTEMPTS:
                    delay = RETRY_BACKOFF_SECONDS * (2**attempt)
                    logger.warning("网关限频（429），%.0f 秒后重试：%s", delay, action)
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise GapiError(f"{action} 调用失败：{exc}") from exc
            break

        error = body.get("error") or {}
        if error.get("code") not in (0, None):
            raise GapiError(f"{action} 返回错误：{error}")
        return body.get("data") or {}

    async def list_sessions(self, statuses: tuple[str, ...] = ("预展", "进行中")) -> list[SessionCard]:
        """抓当前专场：实时（mode=1）+ 限时（mode=2），按专场 id 去重。"""
        cards: list[SessionCard] = []
        seen: set[str] = set()
        for mode in (1, 2):
            data = await self._call(ACTION_LIST, {"auction_mode": mode})
            for item in ((data.get("auctions") or {}).get("items")) or []:
                card = card_from_auction(item, tz=self._cfg.app.tz, mode=mode)
                if card and card.mxiqi_id not in seen:
                    seen.add(card.mxiqi_id)
                    cards.append(card)

        wanted = {"preview", "running"} if set(statuses) & {"预展", "进行中"} else set()
        if wanted:
            cards = [card for card in cards if card.status in wanted]
        return cards

    async def list_categories(self) -> list[CategoryInfo]:
        """分类字典：`cates[]` 一级 + 其 `sub[]` 二级，拍平成一行一个分类。"""
        data = await self._call(ACTION_CATE, {})
        rows: list[CategoryInfo] = []
        for item in data.get("cates") or []:
            top = category_from_cate(item)
            if top is None:
                continue
            rows.append(top)
            for sub in item.get("sub") or []:
                child = category_from_cate(sub, parent_code=top.code)
                if child is not None:
                    rows.append(child)
        return rows

    async def session_detail(
        self, mxiqi_id: str, *, fallback: SessionCard | None = None
    ) -> SessionDetail:
        data = await self._call(ACTION_GET, {"auction_id": int(mxiqi_id)})
        detail = detail_from_payload(data, fallback, tz=self._cfg.app.tz)
        auction = data.get("auction") or {}
        kind = SESSION_KIND_BY_MODE.get(auction.get("auction_mode")) or (
            fallback.kind if fallback else None
        )
        # 实时专场取开拍时间（start_time），限时专场取截拍时间（end_time）；
        # UTC ISO 在这里补（纯函数拿不到业务时区）
        at_raw = auction.get("start_time") if kind == KIND_LIVE else auction.get("end_time")
        end_at = to_utc_iso(at_raw, self._cfg.app.tz)
        return replace(detail, end_at=end_at)

    async def search_sold(
        self, keyword: str, cate_id_1: int | None = None, page: int = 1
    ) -> list[SoldSample]:
        """按关键词搜站内**历史成交**（推荐价的证据来源）。

        `keywords` 是关键词字段、`status=2` 是成交（实测 2026-10-12）；
        空格分隔的多词也认（我们的关键词就是「英国 1960 纸币」这种）。
        `cate_id_1` 进 `search_params` 按一级分类收窄；`page` 放**顶层**
        （`page_size` 被站点忽略，固定 21 条/页，plan §18.19.1）。
        命中 0 条是合法结果，不是错误。
        """
        keyword = keyword.strip()
        if not keyword:
            return []
        search_params: dict = {"keywords": keyword, "status": SOLD_STATUS}
        if cate_id_1:
            search_params["cate_id_1"] = cate_id_1
        data = await self._call(
            ACTION_FILTER, {"search_params": search_params, "page": page}
        )
        return sold_from_payload(data, self._cfg.app.tz)

    async def search_filter_items(self, search_params: dict, page: int) -> list[dict]:
        """`miniprogram.home.filter` 的原始条目（热度回填用，不做字段映射）。

        `search_params` 原样透传（调用方负责 `cate_id_1` / `cate_id_2` / `status`），
        `page` 放顶层；`page_size` 被站点忽略（固定 21 条/页，plan §18.19.1）。
        """
        data = await self._call(
            ACTION_FILTER, {"search_params": search_params, "page": page}
        )
        return ((data.get("auction_items") or {}).get("items")) or []

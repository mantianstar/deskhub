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
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.config import Config
from app.coins.pricing import SoldSample

logger = logging.getLogger(__name__)

# 本模块用到的三个网关动作
ACTION_LIST = "miniprogram.auction.ListAuction"
ACTION_GET = "miniprogram.auction.get"
ACTION_FILTER = "miniprogram.home.filter"

# 专场状态码 → 我们的三态（spec §14.7）
SESSION_STATUS_BY_CODE = {0: "preview", 1: "running", 2: "closed"}
# 专场类型：站内 `auction_mode` 1=实时 / 2=限时
SESSION_KIND_BY_MODE = {1: "live", 2: "timed"}
KIND_LIVE = "live"
# 站内「成交」的 status 取值（同网页 `/auction/item?…&status=成交`）
SOLD_STATUS = 2

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


@dataclass(frozen=True)
class LotInfo:
    mxiqi_id: str
    lot_no: str | None
    title: str
    start_price: float | None
    current_price: float | None
    image_urls: list[str]
    tags: dict[str, str]


@dataclass(frozen=True)
class SessionDetail:
    card: SessionCard
    deposit: float | None
    lots: list[LotInfo]
    # 截拍时间的 UTC ISO8601（站内是北京时间；落库口径统一用 UTC）
    end_at: str | None = None


# ---------------------------------------------------------------- 纯函数：解析


def _money(value: object) -> float | None:
    """"分" → "元"。站点价格一律是整数分。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value) / 100, 2)
    except (TypeError, ValueError):
        return None


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
    )


def lot_from_item(item: dict, *, oss_base: str = DEFAULT_OSS_BASE) -> LotInfo | None:
    """`auction_items.items` 的一条拍品 → `LotInfo`。"""
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
    return LotInfo(
        mxiqi_id=str(item_id),
        lot_no=f"Lot.{order_no}" if order_no else None,
        title=title,
        start_price=_money(item.get("price_start")),
        current_price=_money(item.get("price")),
        image_urls=_images(oss_base, item.get("thumbs"), 3),
        tags=tags,
    )


def detail_from_payload(data: dict, fallback: SessionCard | None = None) -> SessionDetail:
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
    lots = [lot for lot in (lot_from_item(item, oss_base=oss_base) for item in items) if lot]

    cover_urls = _images(oss_base, (items[0].get("thumbs") if items else None), 4) or (
        list(fallback.cover_urls) if fallback else []
    )
    kind = SESSION_KIND_BY_MODE.get(auction.get("auction_mode")) or (
        fallback.kind if fallback else None
    )
    at_raw = auction.get("start_time") if kind == KIND_LIVE else auction.get("end_time")
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
    )


def sold_from_payload(data: dict, tz) -> list[SoldSample]:
    """`miniprogram.home.filter` 的 data → 成交样本（title / price / sold_at）。

    只取 `status == SOLD_STATUS`（成交）的条目：`price` 是成交价（分），
    `end_time` 是这条拍品的成交时间（当 `sold_at` 用，供 180 天窗口过滤）。
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
        samples.append(
            SoldSample(title=title, price=price, sold_at=to_utc_iso(item.get("end_time"), tz))
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

    async def session_detail(
        self, mxiqi_id: str, *, fallback: SessionCard | None = None
    ) -> SessionDetail:
        data = await self._call(ACTION_GET, {"auction_id": int(mxiqi_id)})
        detail = detail_from_payload(data, fallback)
        auction = data.get("auction") or {}
        kind = SESSION_KIND_BY_MODE.get(auction.get("auction_mode")) or (
            fallback.kind if fallback else None
        )
        # 实时专场取开拍时间（start_time），限时专场取截拍时间（end_time）；
        # UTC ISO 在这里补（纯函数拿不到业务时区）
        at_raw = auction.get("start_time") if kind == KIND_LIVE else auction.get("end_time")
        end_at = to_utc_iso(at_raw, self._cfg.app.tz)
        return SessionDetail(
            card=detail.card, deposit=detail.deposit, lots=detail.lots, end_at=end_at
        )

    async def search_sold(self, keyword: str) -> list[SoldSample]:
        """按关键词搜站内**历史成交**（推荐价的证据来源）。

        `keywords` 是关键词字段、`status=2` 是成交（实测 2026-10-12）；
        空格分隔的多词也认（我们的关键词就是「英国 1960 纸币」这种）。
        命中 0 条是合法结果，不是错误。
        """
        keyword = keyword.strip()
        if not keyword:
            return []
        data = await self._call(
            ACTION_FILTER, {"search_params": {"keywords": keyword, "status": SOLD_STATUS}}
        )
        return sold_from_payload(data, self._cfg.app.tz)

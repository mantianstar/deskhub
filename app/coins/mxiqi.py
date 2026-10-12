"""麦稀奇站点采集（spec §14.3 / plan §18.3）—— 全项目**唯一**耦合站点的文件。

站点事实（2026-10-09 实测）：
- 站点是 JS 单页应用，且全站（专场/详情/成交搜索）对非浏览器请求返回约 1.3KB 的
  阿里云滑块验证页（`captchaV2.js`）。httpx 路径不可用，故这里用 Playwright。
- 抓取用**持久化上下文**：人工过一次滑块后会话落盘（`config.coins.profile_dir`），
  之后 headless 复用；被拦则判 `blocked`，由上层显著提示（spec §14.3，不静默失败）。

设计取舍：
- 解析逻辑（`parse_*`）是**纯函数**，只吃 HTML，便于用离线 fixture 回归；
  `MxiqiClient` 只负责「渲染 → 取 HTML → 交给纯函数」，站点改版时改这里。
- 另有一条降级路径：`--dump` 把渲染后的 HTML 存进 `data/mxiqi-inbox/`，
  解析器也能直接从本地文件吃（站点结构大改时可人工导出后调试）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from app.config import Config
from app.coins.pricing import SoldSample

logger = logging.getLogger(__name__)

# 反爬拦截判定。
# **不要**用 `captchaV2` / `custom-slider` 当特征 —— 实测它们出现在**每一个**页面的
# 脚本引用里（防爬 SDK 全站加载），拿它判定会把正常页面全判成被拦（2026-10-12 实测踩到）。
# 可靠特征只有「验证弹窗」自己的痕迹：
BLOCKED_MAX_BYTES = 4000
CAPTCHA_MARKERS = (
    "<title>Verification</title>",
    '"Please complete the operation to verify that you are a real person."',
    "请滑动验证",
    "请完成验证",
)

# 页面就绪的等待策略（`_wait_ready`）：
# - SPA 内容是异步渲染的，所以**必须轮询**着看，不能 goto 完就判一次。
# - headless：只给有限的等待，超时即判 blocked（无常驻人工可用）。
# - headful：留足时间让你在弹出的浏览器里手动过滑块，期间每 3 秒重看一次。
POLL_SECONDS = 3
CONTENT_GRACE_SECONDS = 20
HEADFUL_VERIFY_SECONDS = 180
# JS 填次要内容（价格/徽标）的等待：networkidle 尽力等 + 固定再停几秒
NETWORKIDLE_TIMEOUT_MS = 15000
SETTLE_SECONDS = 3

# 「成交」在站内搜索里的取值（实测 `status=2` 为已成交）
STATUS_CODES = {"成交": "2"}

# 降低自动化特征（属于用户 2026-10-09 批准的「本模块允许反爬」范围：
# 让浏览器别一眼被认出来，滑块仍由人工过 —— 不是自动破解验证码）。
LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
)
# 抹掉最常被检测的 `navigator.webdriver`
STEALTH_INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"

SESSION_STATUS_MARKERS = (
    ("进行中", "running"),
    ("预展中", "preview"),
    ("预出价", "preview"),
    ("预展", "preview"),
    ("公示", "closed"),
    ("已结束", "closed"),
)

# ---------------------------------------------------------------- 真实 DOM 锚点
# 下面的正则**全部对着 2026-10-12 用 CDP 抓到的真实 HTML 校准**（样本见 data/mxiqi-inbox/，
# 测试 fixture 已按它更新）。不靠「猜最长文本节点」那种脆弱做法。
#
# 列表页一张卡：
#   <div class="auction" ...>
#     <a title="专场名" href="/auction.info.entry/327686">
#       <span ...>预出价</span><span style="margin-left:5px">11月1日 星期日 04:00</span>
#       <h2 class="twoline">专场名</h2></a>
#     <a title="店铺名" href="/user.main.entry/101298"><img src=".../USR/..."></a>
#     <a href="/auction.info.entry/327686">
#       <div class="img"><img src=".../AUC/..."></div> ×4
#       <div>共57件拍品</div><div>3982人次围观</div></a>
#   </div>
#
# 详情页一条拍品：
#   <li id="item6871280">
#     <a href="/auction.item.info/6871280" title="拍品名">
#       <span class="num">Lot.1</span><h6 class="col item_name">拍品名</h6></a>
#     <span class="col price">起价：0元</span>
#     <span class="col center start_1_price">当前价：0元</span>
#     <div class="thumb col"><img data-original-big="..." src=".../AUC/..."></div>
#   </li>

_A_TAG_RE = re.compile(r"<a\b[^>]*>", re.I)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')

_CARD_SPLIT_RE = re.compile(r'(?=<div class="auction")')
_LOT_SPLIT_RE = re.compile(r'(?=<li id="item\d+")')

_ENTRY_HREF_RE = re.compile(r"/auction\.info\.entry/(\d+)")
_ITEM_HREF_RE = re.compile(r"/auction\.item\.info/(\d+)")
_SHOP_HREF_RE = re.compile(r"/user\.main\.entry/(\d+)")

# 时间只写成「11月1日 星期日 04:00」，状态在另一个 <span> 里（所以不能要求前缀）
_DATE_TEXT_RE = re.compile(r"\d{1,2}月\d{1,2}日\s*星期.\s*\d{1,2}:\d{2}")
_LOT_TOTAL_LIST_RE = re.compile(r"共\s*(\d+)\s*件拍品")
# 详情页拍品总数写成「共57件」
_LOT_TOTAL_DETAIL_RE = re.compile(r"共\s*(\d+)\s*件")
_VIEW_RE = re.compile(r"(\d+)\s*人次围观")
_DEPOSIT_RE = re.compile(r'class="min"[^>]*>\s*押\s*</span>\s*[¥￥]\s*([\d,]+(?:\.\d+)?)')

_NUM_RE = re.compile(r'<span class="num">\s*(Lot[.．]\s*\d+)\s*</span>')
_LOT_ID_RE = re.compile(r'<li id="item(\d+)"')
_ATTR_BLOCK_RE = re.compile(r'<div class="attr col"[^>]*>(.*?)</div>', re.S)
_TAG_PAIR_RE = re.compile(r"【(.+?)】\s*([^<\s【]{1,20})")
_START_PRICE_RE = re.compile(r'<span class="col price">\s*起价\s*[:：]\s*[¥￥]?\s*([\d,]+(?:\.\d+)?)')
_CURRENT_PRICE_RE = re.compile(
    r'<span class="col center start_\d+_price">\s*当前价\s*[:：]\s*[¥￥]?\s*([\d,]+(?:\.\d+)?)'
)
# 认证 / 保证金 / N年店铺：<span class="wrap">…<span>认证</span>
_BADGE_SPAN_RE = re.compile(r'<span class="wrap">.*?<span>([^<]+)</span>', re.S)
_THUMB_BLOCK_RE = re.compile(r'<div class="thumb col".*?(?=<div class="(?:attr|desc)|</li>)', re.S)

# 成交搜索页：条目形如
#   <a title="专场名 - 拍品名" href="/auction.item.info/1870684">
#     …<div class="sold-price" item_id="1870684">¥100 <span>成交</span></div>
# 价格**不在初始 HTML 里**：页面加载后自己 `POST /auction.item.prices` 拿 JSON 再填进
# `.sold-price` 节点；未登录时那句是「登录后查看成交价」。
#
# **不要自己发那个 POST**：实测用 fetch 或页面自带的 jQuery 发，都会被阿里云 WAF 拦下
# （HTTP 200 但返回 WAF 页面），而**页面自己发的请求是通的**。所以这里只读渲染后的 DOM。
_SOLD_ITEM_HREF_RE = re.compile(r"/auction\.item\.info/(\d+)")
_SOLD_PRICE_RE = re.compile(
    r'<div class="sold-price"[^>]*item_id="(\d+)"[^>]*>(.*?)</div>', re.S
)
_PRICE_NUM_RE = re.compile(r"[¥￥]\s*([\d,]+(?:\.\d+)?)")
SOLD_LOGIN_REQUIRED_TEXT = "登录后查看成交价"


class MxiqiError(RuntimeError):
    """采集过程中的非拦截类错误（HTTP / 解析）。"""


class BlockedError(RuntimeError):
    """被反爬拦下（会话失效）。上层据此记 `blocked` 并提示人工过一次验证。"""


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
    auction_at_raw: str | None
    lot_total: int | None
    view_count: int | None
    cover_urls: list[str]
    shop: ShopInfo | None = None


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


# ---------------------------------------------------------------- 纯函数：判定与解析


def looks_blocked(html: str) -> bool:
    """页面上是不是「被反爬拦下」的验证页。"""
    if not html:
        return True
    if len(html.encode("utf-8")) <= BLOCKED_MAX_BYTES:
        return True
    return any(marker in html for marker in CAPTCHA_MARKERS)


def looks_usable(html: str, expect: tuple[str, ...] = ()) -> bool:
    """页面是否已经渲染出可用内容：没被拦、且出现了「本该有的内容」。

    SPA 加载是异步的，刚拿到 `domcontentloaded` 时内容常常还没渲染 —— 所以这个判定
    要放在轮询里反复看，不能只看一次（见 `MxiqiClient._wait_ready`）。
    """
    if looks_blocked(html):
        return False
    return not expect or any(token in html for token in expect)


def strip_tags(html: str) -> str:
    """去标签后的可见文本（用空白替换标签，保留词边界）。"""
    return re.sub(r"<[^>]+>", " ", html)


def _attrs(tag: str) -> dict[str, str]:
    """把一个标签串里的属性抽成 dict（供按 `title=` / `href=` 取值）。"""
    return {key.lower(): value for key, value in _ATTR_RE.findall(tag)}


def _image_url(tag: str) -> str | None:
    """图片地址：优先 `data-original-big`（原图），回退 `src`。"""
    attrs = _attrs(tag)
    return attrs.get("data-original-big") or attrs.get("src")


def _images_in(segment: str, *, kind: str, limit: int) -> list[str]:
    """取片段里的图片地址。`kind` 用来区分资源桶：`AUC` = 拍品图，`USR` = 头像。"""
    urls: list[str] = []
    for tag in _IMG_TAG_RE.findall(segment):
        url = _image_url(tag)
        if not url or f"/{kind}/" not in url or url in urls:
            continue
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _anchor_attrs_for(segment: str, href_re: re.Pattern[str]) -> tuple[str, dict[str, str]] | None:
    """找第一个 `href` 命中 `href_re` 的 `<a>`，返回 `(href, 属性 dict)`。"""
    for tag in _A_TAG_RE.findall(segment):
        attrs = _attrs(tag)
        href = attrs.get("href", "")
        if href_re.search(href):
            return href, attrs
    return None


def _to_price(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _status_of(html: str) -> str | None:
    """专场状态：站内是只含状态词的 `<span>`（列表页「预出价」、详情页「预展中」）。"""
    text = strip_tags(html)
    for marker, code in SESSION_STATUS_MARKERS:
        if marker in text:
            return code
    return None


def _badges_from(segment: str) -> list[dict]:
    """认证 / 保证金 / N年店铺：结构是 `<span class="wrap">…<span>认证</span>`。

    用结构而不是纯文本匹配，避免页面描述文字里的「保证金」被误当成徽标。
    """
    badges: list[dict] = []
    for label in _BADGE_SPAN_RE.findall(segment):
        label = label.strip()
        if not label:
            continue
        item = {"kind": "years" if label.endswith("年店铺") else label, "label": label}
        if item not in badges:
            badges.append(item)
    return badges


# 店铺信息（头像/徽标）在店铺锚点之后的这一段里
_SHOP_BLOCK_CHARS = 2500


def _shop_from(segment: str) -> ShopInfo | None:
    """店铺：`<a title="店铺名" href="/user.main.entry/{uid}">` + 其后的头像与徽标。"""
    found = _anchor_attrs_for(segment, _SHOP_HREF_RE)
    if found is None:
        return None
    href, attrs = found
    uid_match = _SHOP_HREF_RE.search(href)
    idx = segment.find(href)
    window = segment[idx : idx + _SHOP_BLOCK_CHARS] if idx != -1 else segment

    avatars = _images_in(window, kind="USR", limit=1)
    name = attrs.get("title") or _first_text(window, "h3")
    if uid_match is None and not name:
        return None
    return ShopInfo(
        mxiqi_uid=uid_match.group(1) if uid_match else None,
        name=name,
        avatar_url=avatars[0] if avatars else None,
        badges=_badges_from(window),
    )


def _first_text(segment: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}\b[^>]*>([^<]+)</{tag}>", segment, re.I)
    return match.group(1).strip() if match else None


def parse_sessions(html: str) -> list[SessionCard]:
    """解析专场列表页（`/auction`）—— 按 `<div class="auction">` 切卡。"""
    cards: list[SessionCard] = []
    for chunk in _CARD_SPLIT_RE.split(html):
        found = _anchor_attrs_for(chunk, _ENTRY_HREF_RE)
        if found is None:
            continue
        href, attrs = found
        id_match = _ENTRY_HREF_RE.search(href)
        if id_match is None:
            continue
        title = attrs.get("title") or _first_text(chunk, "h2") or ""
        if not title.strip():
            continue

        text = strip_tags(chunk)
        date_match = _DATE_TEXT_RE.search(text)
        total_match = _LOT_TOTAL_LIST_RE.search(text)
        view_match = _VIEW_RE.search(text)
        cards.append(
            SessionCard(
                mxiqi_id=id_match.group(1),
                title=title.strip(),
                status=_status_of(chunk),
                auction_at_raw=date_match.group(0).strip() if date_match else None,
                lot_total=int(total_match.group(1)) if total_match else None,
                view_count=int(view_match.group(1)) if view_match else None,
                cover_urls=_images_in(chunk, kind="AUC", limit=4),
                shop=_shop_from(chunk),
            )
        )
    return cards


def parse_session_detail(
    html: str,
    mxiqi_id: str,
    *,
    fallback: SessionCard | None = None,
) -> SessionDetail:
    """解析专场详情页（`/auction.info.entry/{id}`）—— 锚点按真实 DOM 校准。

    详情页拿不到的字段用列表页那张卡 `fallback` 兜底：
    - **围观数**：详情页是 `<span id="pv"></span>`，值由 JS 另发请求填，静态 DOM 里没有；
    - **封面图**：详情页没有列表页那 4 张封面。
    两边都没有就留 None，不瞎猜。
    """
    text = strip_tags(html)
    date_match = _DATE_TEXT_RE.search(text)
    total_match = _LOT_TOTAL_DETAIL_RE.search(text)
    deposit_match = _DEPOSIT_RE.search(html)

    title = _first_text(html, "h1") or (fallback.title if fallback else None) or f"专场 {mxiqi_id}"
    card = SessionCard(
        mxiqi_id=mxiqi_id,
        title=title,
        status=_status_of(html) or (fallback.status if fallback else None),
        auction_at_raw=(date_match.group(0).strip() if date_match else None)
        or (fallback.auction_at_raw if fallback else None),
        lot_total=(int(total_match.group(1)) if total_match else None)
        or (fallback.lot_total if fallback else None),
        view_count=fallback.view_count if fallback else None,
        cover_urls=list(fallback.cover_urls) if fallback else [],
        shop=_shop_from(html) or (fallback.shop if fallback else None),
    )
    return SessionDetail(
        card=card,
        deposit=_to_price(deposit_match.group(1)) if deposit_match else None,
        lots=parse_lots(html),
    )


def _lot_title(chunk: str) -> str | None:
    for tag in _A_TAG_RE.findall(chunk):
        attrs = _attrs(tag)
        if _ITEM_HREF_RE.search(attrs.get("href", "")):
            return attrs.get("title") or None
    return _first_text(chunk, "h6")


def _lot_tags(chunk: str) -> dict[str, str]:
    """拍品标签（如【发行年份】1808）在 `<div class="attr col">` 里。

    ⚠️ 抓到的样本里这个块是空的（多数专场没有标签），所以这条分支**没有真实样本验证过**，
    等遇到带标签的专场再用 `coins-dump` 校正。
    """
    block = _ATTR_BLOCK_RE.search(chunk)
    if block is None:
        return {}
    return {key.strip(): value.strip() for key, value in _TAG_PAIR_RE.findall(block.group(1))}


def parse_lots(html: str) -> list[LotInfo]:
    """抽拍品：按 `<li id="item{拍品id}">` 切块 —— 每块就是一条拍品。

    站内拍品 id 直接写在 `<li id="item...">` 里，拿它当去重键；比之前「按 `Lot.N` 切 +
    序号拼 id」稳得多（旧写法会把同一条拍品重复计数，实测 495 条里大量重复）。
    """
    lots: list[LotInfo] = []
    for chunk in _LOT_SPLIT_RE.split(html):
        id_match = _LOT_ID_RE.search(chunk)
        if id_match is None:
            continue
        title = _lot_title(chunk)
        if not title:
            continue
        num_match = _NUM_RE.search(chunk)
        start_match = _START_PRICE_RE.search(chunk)
        current_match = _CURRENT_PRICE_RE.search(chunk)
        thumb = _THUMB_BLOCK_RE.search(chunk)
        lots.append(
            LotInfo(
                mxiqi_id=id_match.group(1),
                lot_no=num_match.group(1).replace("．", ".") if num_match else None,
                title=title.strip(),
                start_price=_to_price(start_match.group(1)) if start_match else None,
                current_price=_to_price(current_match.group(1)) if current_match else None,
                image_urls=_images_in(thumb.group(0), kind="AUC", limit=3) if thumb else [],
                tags=_lot_tags(chunk),
            )
        )
    return lots


def parse_sold(html: str) -> list[SoldSample]:
    """从**渲染后**的成交搜索页读成交价（价格由页面自己 POST 后填进 `.sold-price`）。

    只读 DOM、不自己发请求 —— 原因见上面常量区的注释（自己发会被 WAF 拦）。
    """
    titles = dict(parse_sold_items(html))
    samples: list[SoldSample] = []
    for match in _SOLD_PRICE_RE.finditer(html):
        item_id = match.group(1)
        number = _PRICE_NUM_RE.search(strip_tags(match.group(2)))
        if number is None:
            continue
        price = _to_price(number.group(1))
        if price is None:
            continue
        samples.append(SoldSample(title=titles.get(item_id) or f"拍品 {item_id}", price=price))
    return samples


def sold_page_needs_login(html: str) -> bool:
    """成交搜索页是否在提示「登录后查看成交价」。"""
    return SOLD_LOGIN_REQUIRED_TEXT in html


def parse_sold_items(html: str) -> list[tuple[str, str]]:
    """成交搜索页里的 `(拍品 id, 标题)` 列表 —— 价格由页面自己填进 `.sold-price`。"""
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tag in _A_TAG_RE.findall(html):
        attrs = _attrs(tag)
        match = _SOLD_ITEM_HREF_RE.search(attrs.get("href", ""))
        if match is None:
            continue
        item_id = match.group(1)
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append((item_id, (attrs.get("title") or "").strip()))
    return items


# ---------------------------------------------------------------- Playwright 客户端


class MxiqiClient:
    """Playwright 持久化会话的薄封装；用法：`async with MxiqiClient(cfg) as client:`。"""

    def __init__(self, config: Config, *, cdp_url: str | None = None) -> None:
        self._cfg = config
        self._playwright = None
        self._context = None
        self._page = None
        self.blocked = False
        # 传了就用传的（CLI --cdp），否则用配置里的
        self._cdp_url = (cdp_url if cdp_url is not None else config.coins.cdp_url).strip()

    @property
    def interactive(self) -> bool:
        """走 CDP 或 headful 时，视为「有人能手动过滑块」。"""
        return bool(self._cdp_url) or not self._cfg.coins.headless

    async def __aenter__(self) -> "MxiqiClient":
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给明确指引
            raise MxiqiError("未安装 playwright：`pip install playwright && playwright install chromium`") from exc

        self._playwright = await async_playwright().start()

        if self._cdp_url:
            # 连本机已打开的 Chrome：页面跑在**真实浏览器**里，滑块由人工在那过；
            # 退出时**不关浏览器**（那是用户自己的），只断开连接 —— plan §18.3。
            try:
                browser = await self._playwright.chromium.connect_over_cdp(self._cdp_url)
            except Exception as exc:  # noqa: BLE001 - 连不上要给可操作的提示
                raise MxiqiError(
                    f"连不上本机 Chrome（{self._cdp_url}）：{exc}\n"
                    "请先完全退出 Chrome，再用 `--remote-debugging-port=9222` 启动它（见 README）"
                ) from exc
            self._context = (
                browser.contexts[0] if browser.contexts else await browser.new_context()
            )
            self._page = await self._context.new_page()
            logger.info("已连上本机 Chrome（CDP）：%s", self._cdp_url)
            return self

        profile_dir = self._cfg.coins.profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)
        launch_kwargs: dict = {
            "user_data_dir": str(profile_dir),
            "headless": self._cfg.coins.headless,
            "timeout": self._cfg.coins.session_timeout_seconds * 1000,
            "args": list(LAUNCH_ARGS),
            "locale": "zh-CN",
            "timezone_id": self._cfg.app.timezone,
            "viewport": {"width": 1280, "height": 900},
        }
        if self._cfg.coins.browser_channel:
            launch_kwargs["channel"] = self._cfg.coins.browser_channel
        self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        await self._context.add_init_script(STEALTH_INIT_SCRIPT)
        self._page = await self._context.new_page()
        logger.info(
            "麦稀奇会话就绪：profile_dir=%s headless=%s channel=%s",
            profile_dir,
            self._cfg.coins.headless,
            self._cfg.coins.browser_channel or "bundled-chromium",
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._cdp_url:
            # 只断开连接，绝不 close：那是用户自己开着的浏览器
            if self._page is not None:
                await self._page.close()
            if self._playwright is not None:
                await self._playwright.stop()
            return
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def _render(self, url: str, *, expect: tuple[str, ...] = ()) -> str:
        """渲染一个页面并返回 DOM HTML；被拦时抛 `BlockedError`。

        `expect` 是「这个页面**本该**出现的内容」：都没有出现就说明页面没渲染出数据
        （SPA 未加载 / 被静默拦截），按 `blocked` 处理而不是当成空结果 —— 否则解析器会
        把空壳当成「今天没有专场」。（实测被拦页 54KB、不含任何 `auction.info.entry` 链接。）
        """
        page = self._page
        if page is None:  # pragma: no cover - 只有误用才走到
            raise MxiqiError("客户端尚未通过 async with 打开")
        logger.info("麦稀奇渲染：%s", url)
        # 用 domcontentloaded 而不是 networkidle：验证页会挂着长连接，networkidle 容易干等；
        # 内容是否就绪交给后面的轮询判定。
        await page.goto(
            url, wait_until="domcontentloaded", timeout=self._cfg.coins.session_timeout_seconds * 1000
        )
        await self._wait_ready(page, url, expect)
        await self._settle(page)
        html = await page.content()
        if not looks_usable(html, expect):
            self.blocked = True
            raise BlockedError(f"被反爬拦下（{len(html)} 字节）：{url}")
        # 限速：给站点的请求之间留间隔（plan §18.3）
        await asyncio.sleep(self._cfg.coins.request_interval_seconds)
        return html

    async def _settle(self, page) -> None:
        """等 JS 把次要内容填完。

        实测（2026-10-12）：`domcontentloaded` 时详情页**只有拍品目录**，起价 / 当前价 /
        认证徽标都是之后才填进去的 —— 不 settle 就会全部解析成 None。
        """
        try:
            await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - 长连接页面永远不 idle，超时就按已渲染内容继续
            logger.debug("networkidle 未在超时内到达，按已渲染内容继续")
        await asyncio.sleep(SETTLE_SECONDS)

    async def _wait_ready(self, page, url: str, expect: tuple[str, ...]) -> str:
        """轮询等页面渲染出可用内容；有真人可操作（headful / CDP）时留时间手动过滑块。"""
        interactive = self.interactive
        budget = HEADFUL_VERIFY_SECONDS if interactive else CONTENT_GRACE_SECONDS
        deadline = time.monotonic() + budget
        hinted = False
        while True:
            html = await page.content()
            if looks_usable(html, expect):
                return html
            if time.monotonic() >= deadline:
                self.blocked = True
                raise BlockedError(f"被反爬拦下（{len(html)} 字节）：{url}")
            if interactive and not hinted:
                logger.warning(
                    "检测到验证页：请在弹出的浏览器窗口里手动过一次滑块（最多等 %d 秒）",
                    HEADFUL_VERIFY_SECONDS,
                )
                hinted = True
            await asyncio.sleep(POLL_SECONDS)

    def _url(self, path: str) -> str:
        return f"{self._cfg.coins.base_url.rstrip('/')}{path}"

    async def list_sessions(self, statuses: tuple[str, ...] = ("预展", "进行中")) -> list[SessionCard]:
        """抓专场列表。`statuses` 只用于过滤返回结果的状态。"""
        html = await self._render(self._url("/auction"), expect=("/auction.info.entry/",))
        cards = parse_sessions(html)
        if statuses:
            wanted = {"preview", "running"} if set(statuses) & {"预展", "进行中"} else set()
            if wanted:
                cards = [card for card in cards if card.status in wanted]
        return cards

    async def session_detail(
        self, mxiqi_id: str, *, fallback: SessionCard | None = None
    ) -> SessionDetail:
        html = await self._render(
            self._url(f"/auction.info.entry/{mxiqi_id}"), expect=("item_name",)
        )
        return parse_session_detail(html, mxiqi_id, fallback=fallback)

    async def search_sold(self, keyword: str) -> list[SoldSample]:
        """按关键词搜站内历史成交（推荐价的证据来源）。

        ⚠️ 价格要**登录**才能看：未登录时页面显示「登录后查看成交价」。
        处理方式按 spec §14.3：**人工在真实浏览器里登录一次、只复用会话**，程序不碰密码。

        价格**由页面自己请求后填进 DOM**，这里只读渲染结果（自己发那个 POST 会被 WAF 拦）。
        命中 0 条是合法结果，不判成 blocked。
        """
        status = self._cfg.coins.price.status
        code = STATUS_CODES.get(status, status)
        html = await self._render(
            self._url(f"/auction/item?keyword={quote(keyword, safe='')}&status={code}")
        )
        if sold_page_needs_login(html):
            raise MxiqiError(
                "成交价要登录才能看（页面显示「登录后查看成交价」）。"
                "请确认你登录的是**用 --user-data-dir 启动的那个 Chrome 窗口**里的麦稀奇账号"
                "（那不是你平时的 Chrome profile，需要单独登录一次）。"
            )
        return parse_sold(html)

    async def dump(self, url: str, target: str) -> Path:
        """把渲染后的 HTML 存到 `data/mxiqi-inbox/`（站点改版时用真实样本校正解析器）。"""
        html = await self._render(url)
        inbox = self._cfg.coins.profile_dir.parent / "mxiqi-inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"{target}.html"
        path.write_text(html, encoding="utf-8")
        return path

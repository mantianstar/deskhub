"""Numista 网页取数 —— 类型页（估值 + Frequency）与目录筛选列表页（候选池）。

类型页部分（spec §14.13 / plan §18.13.2）：为什么走页面而不是接口：`GET /types/{id}/issues` 只有
`id / year / gregorian_year / mintage / comment / mint_letter / references / marks` ——
**既没有**各品相估值，**也没有** Frequency。这两列只出现在类型页的表格里
（`en.numista.com/{type_id}`）。Frequency 的口径（页面原文）：

    Frequencies show the percentage of Numista users who own each year or variety
    among all the users who own this coin.

目录列表页部分（spec §14.17.4）：`/catalogue/index.php` 支持年份 / 材质 / 直径等服务端筛选，
列表里直接带材质、重量、直径，比接口 `?q=` 的模糊搜索更准，也不用逐条取详情再过滤。

站点事实（2026-10-14 实测，反直觉处必须记下）：

- 表格是**服务端渲染**的，数据直接躺在 HTML 里；但 numista.com 前面挡着 Cloudflare 的
  JS 校验页（`curl` / `httpx`（带完整浏览器头）/ `curl_cffi`（chrome、safari 指纹伪装）
  **一律 403**，返回 `Checking connection…` + `challenge-platform`）。
- **只有真实浏览器能过**：Playwright 自己 launch 的 Chrome（headless / headful 都试过）
  卡在校验页；而**用调试端口开一个正常 Chrome、再用 CDP 挂上去**能正常拿到页面。
  故本模块只用 `connect_over_cdp`（不 launch、不加自动化开关），也因此**不需要**
  `playwright install`（用的是你自己的 Chrome）。
- 表格结构：每行 `<tr class="date_row">`；估值在
  `<td class="value" data-grade="G|VG|F|VF|XF|AU|UNC">`；Frequency 在
  `<td class="own_stat"><span class="cluster3">27%</span>`；行**后面**紧跟的
  `<tbody id="collec_line{单版编号}">` 就是 Numista 单版编号 —— 与接口的 issue id 一一对应
  （实测系列 670：15 行 15 个编号，顺序也与接口一致）。
- 估值按**访客币种**渲染，币种写在表下：
  `Values in the table above are expressed in <strong>CNY</strong>` —— 所以币种是**读出来的**，
  不是我们指定的（想钉死币种就得走接口的 `?currency=`，但接口没有 Frequency）。

设计取舍：解析是**纯函数**（只吃 HTML 字符串，离线可测），`fetch_type_page` 只负责
「浏览器 → HTML」，`collect_issue_values` 负责落库。
"""

from __future__ import annotations

import html as htmllib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from app import repository
from app.config import PROJECT_ROOT, Config, ExploreCatalogueConfig
from app.models import CoinIssueValues

logger = logging.getLogger(__name__)

# 类型页（系列详情页）；也是落库时 `series_numista_id` 的来源
TYPE_PAGE_URL = "https://en.numista.com/{type_id}"
# 目录筛选列表页（候选池）：筛选参数见 `ExploreCatalogueConfig`
CATALOGUE_INDEX_URL = "https://en.numista.com/catalogue/index.php"

# 页面列名（`data-grade` 是大写，存库列是 `value_*` 小写）
GRADES = ("G", "VG", "F", "VF", "XF", "AU", "UNC")

# 页面标记（实测结构，改版时改这一处）
_ISSUE_MARKER = re.compile(r'<tbody[^>]*id="collec_line(\d+)"')
_ROW_MARKER = 'class="date_row"'
# 等表格出现用的 CSS 选择器（`_ROW_MARKER` 是给 HTML 字符串用的子串）
_ROW_SELECTOR = "tr.date_row"
_DATE = re.compile(r'class="date">\s*(.*?)\s*</td>', re.S)
_VALUE = re.compile(
    r'class="value"\s+data-grade="(%s)"\s*>(.*?)</td>' % "|".join(GRADES), re.S
)
_FREQUENCY = re.compile(r'class="own_stat">\s*<span[^>]*>\s*([\d.]+)\s*%')
_CURRENCY = re.compile(r"expressed in\s*<strong>\s*([A-Za-z]{3})\s*</strong>")
_LEADING_INT = re.compile(r"(\d+)")
# Cloudflare 校验页的痕迹（拿到表格时不会出现）
_CHALLENGE = "challenge-platform"
# 数字里可能夹的空白（含 &nbsp; 与窄空格）与千分位
_BLANKS = str.maketrans("", "", " \xa0\u202f\u2009,")

# 目录列表页标记（2026-10-18 实测；改版时改这一处）
_CAT_ITEM = '<div class="resultat_recherche">'
_CAT_CONTAINER = "div.catalogue_search_results"
_CAT_TOTAL = re.compile(r"([\d,]+)\s+results found")
_CAT_ANCHOR = re.compile(r'<strong><a href="/(\d+)"\s*>(.*?)</a></strong>', re.S)
_CAT_GROUP = re.compile(r"<header[^>]*>\s*<h2[^>]*>(.*?)</h2>", re.S)
_CAT_SPEC = re.compile(r"<em>.*?</em>.*?<br>\s*(.*?)<br>", re.S)
_CAT_REF = re.compile(r"<br>\s*([^<]*#[^<]*)</div>", re.S)
_CAT_OBVERSE = re.compile(r'class="photo_avers".*?<img src="([^"]+)"', re.S)
_CAT_REVERSE = re.compile(r'class="photo_revers".*?<img src="([^"]+)"', re.S)
_CAT_WEIGHT = re.compile(r"([\d.]+)\s*g\b")
_CAT_SIZE = re.compile(r"⌀\s*([\d.]+)\s*mm")


class NumistaPageError(RuntimeError):
    """网页取数异常：挂不上 CDP 浏览器 / 卡在 Cloudflare / 结构变了。"""


@dataclass(frozen=True)
class IssueValueInfo:
    """类型页表格的一行：单版编号 + 各品相估值 + Frequency。"""

    numista_issue_id: int
    year: int | None
    value_g: float | None = None
    value_vg: float | None = None
    value_f: float | None = None
    value_vf: float | None = None
    value_xf: float | None = None
    value_au: float | None = None
    value_unc: float | None = None
    frequency: float | None = None


@dataclass(frozen=True)
class TypePageInfo:
    """一个类型页的解析结果：币种 + 各行。"""

    currency: str | None
    rows: list[IssueValueInfo]


@dataclass(frozen=True)
class CatalogueItem:
    """目录列表页的一条结果（筛选已由服务端完成，这里只做解析）。"""

    numista_id: int
    title: str | None
    issuer: str | None
    year_text: str | None
    min_year: int | None
    max_year: int | None
    composition: str | None
    weight_g: float | None
    size_mm: float | None
    catalog_no: str | None
    obverse_picture: str | None
    reverse_picture: str | None


@dataclass(frozen=True)
class CataloguePage:
    """一页（或合并后）的目录结果：总条数 + 每条。"""

    total: int
    items: list[CatalogueItem]


# ---------------------------------------------------------------- 纯函数：解析


def _number(text: str | None) -> float | None:
    """页面单元格 → 数字；空单元格（该品相没有估值）→ None。"""
    if not text:
        return None
    cleaned = text.translate(_BLANKS).strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _year(text: str | None) -> int | None:
    """日期单元格 → 年份（「Undetermined」这类非数字行返回 None）。"""
    match = _LEADING_INT.search(text or "")
    return int(match.group(1)) if match else None


def _parse_row(issue_id: int, row_html: str) -> IssueValueInfo:
    date_match = _DATE.search(row_html)
    frequency_match = _FREQUENCY.search(row_html)
    cells = {
        f"value_{grade.lower()}": _number(text) for grade, text in _VALUE.findall(row_html)
    }
    return IssueValueInfo(
        numista_issue_id=issue_id,
        year=_year(date_match.group(1) if date_match else None),
        frequency=float(frequency_match.group(1)) if frequency_match else None,
        **cells,
    )


def parse_type_page(html: str) -> TypePageInfo:
    """类型页 HTML → （币种, 各行）。

    每行的单版编号取自行后紧跟的 `<tbody id="collec_line{编号}">`；
    编号为 0 的是「Undetermined」占位模板（页面里隐藏），跳过。
    """
    markers = list(_ISSUE_MARKER.finditer(html))
    rows: list[IssueValueInfo] = []
    for index, marker in enumerate(markers):
        issue_id = int(marker.group(1))
        if issue_id == 0:
            continue
        # 这一行的 HTML 在上一个标记与本标记之间（标记**跟在**自己那行后面）
        start = markers[index - 1].end() if index else 0
        segment = html[start : marker.start()]
        row_start = segment.rfind(_ROW_MARKER)
        if row_start < 0:  # 该标记前没有数据行 → 结构变了，跳过而不是臆造
            continue
        rows.append(_parse_row(issue_id, segment[row_start:]))

    currency_match = _CURRENCY.search(html)
    return TypePageInfo(
        currency=currency_match.group(1).upper() if currency_match else None, rows=rows
    )


# ---------------------------------------------------------------- 纯函数：目录列表页


def _plain_text(fragment: str) -> str:
    """HTML 片段 → 纯文本（去标签、解实体、压空白）。"""
    fragment = re.sub(r"<!--.*?-->", " ", fragment, flags=re.S)
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", htmllib.unescape(fragment)).strip()


def catalogue_url(filters: ExploreCatalogueConfig, *, page: int = 1) -> str:
    """按筛选配置拼目录列表页 URL。

    参数名就是站点筛选表单里的 `name`：`st` 类别、`a` 年份、`m` 材质、`t` 直径、
    `p` 页码、`q` 每页条数；其余保持空串，与站点生成的 URL 一致。
    """
    params = {
        "r": "",
        "st": filters.collectible_type,
        "cat": "y",
        "im1": "",
        "im2": "",
        "ru": "",
        "ie": "",
        "no": "",
        "v": "",
        "cu": "",
        "a": filters.year,
        "dg": "",
        "i": "",
        "b": "",
        "m": filters.composition_id,
        "f": "",
        "t": filters.diameter_mm,
        "t2": "",
        "w": "",
        "mt": "",
        "u": "",
        "c": "",
        "wi": "",
        "sw": "",
        "p": page,
        "q": filters.per_page,
    }
    return f"{CATALOGUE_INDEX_URL}?{urlencode(params)}"


def parse_catalogue_index(html: str) -> CataloguePage:
    """目录列表页 HTML → （总条数, 每条）。

    发行方在分组头 `<header><h2>South Africa • Pound (1825-1961)</h2>` 里，
    单条结果自身没有国家字段，所以要按页面顺序继承最近一次分组头。
    """
    total_match = _CAT_TOTAL.search(html)
    total = int(total_match.group(1).replace(",", "")) if total_match else 0

    parts = re.split(f"({re.escape(_CAT_ITEM)})", html)
    items: list[CatalogueItem] = []
    issuer: str | None = None
    for index, part in enumerate(parts):
        headers = list(_CAT_GROUP.finditer(part))
        if headers:
            group = _plain_text(headers[-1].group(1))
            issuer = group.split("•")[0].strip() or None
        if part != _CAT_ITEM or index + 1 >= len(parts):
            continue

        chunk = parts[index + 1]
        anchor = _CAT_ANCHOR.search(chunk)
        if anchor is None:  # 结构变了 → 跳过这一条，不臆造
            continue
        head, _, tail = anchor.group(2).partition("<br>")
        year_text = _plain_text(tail)
        years = [int(value) for value in re.findall(r"\d{3,4}", year_text)]
        spec_match = _CAT_SPEC.search(chunk)
        spec = _plain_text(spec_match.group(1)) if spec_match else ""
        weight_match = _CAT_WEIGHT.search(spec)
        size_match = _CAT_SIZE.search(spec)
        ref_match = _CAT_REF.search(chunk)
        obverse = _CAT_OBVERSE.search(chunk)
        reverse = _CAT_REVERSE.search(chunk)

        items.append(
            CatalogueItem(
                numista_id=int(anchor.group(1)),
                title=_plain_text(head) or None,
                issuer=issuer,
                year_text=year_text or None,
                min_year=years[0] if years else None,
                max_year=years[-1] if years else None,
                composition=(spec.split("•")[0].strip() or None) if spec else None,
                weight_g=_number(weight_match.group(1)) if weight_match else None,
                size_mm=_number(size_match.group(1)) if size_match else None,
                catalog_no=_plain_text(ref_match.group(1)) if ref_match else None,
                obverse_picture=obverse.group(1) if obverse else None,
                reverse_picture=reverse.group(1) if reverse else None,
            )
        )
    return CataloguePage(total=total, items=items)


# ---------------------------------------------------------------- 浏览器取页（CDP）


async def _wait_for_table(page, timeout_seconds: int) -> str:
    """等表格出现（校验页里没有它）；超时给出明确原因。

    用 `wait_for_selector` 而不是轮询 `page.content()`：Cloudflare 校验页会**自我刷新**，
    在导航过程中读内容会抛「page is navigating and changing the content」。
    """
    try:
        await page.wait_for_selector(_ROW_SELECTOR, timeout=timeout_seconds * 1000)
    except Exception:
        pass  # 超时 / 仍在跳转 → 落到下面读一次内容，把原因带进报错

    try:
        html = await page.content()
    except Exception:
        html = ""
    if _ROW_MARKER in html:
        return html
    hint = (
        "；页面停在 Cloudflare 校验页（换一个「非自动化」启动的 Chrome 再试）"
        if _CHALLENGE in html
        else ""
    )
    raise NumistaPageError(f"等类型页表格超时 {timeout_seconds}s{hint}")


def _cdp_hint(cdp_url: str, exc: Exception) -> str:
    """挂不上 CDP 浏览器时的统一提示（页面取数与图片下载共用）。"""
    return (
        f"挂不上 CDP 浏览器 {cdp_url}：{exc}\n"
        "先用调试端口开一个 Chrome：\n"
        '  open -na "Google Chrome" --args --remote-debugging-port=9222'
        ' --user-data-dir="$HOME/.chrome-numista"'
    )


async def fetch_type_page(
    cdp_url: str, type_id: int, *, timeout_seconds: int = 30
) -> str:
    """挂到你自己的 Chrome（CDP）上打开类型页，返回渲染后的 HTML。

    **只**用 `connect_over_cdp`：不 launch 浏览器（Playwright launch 的会被 Cloudflare 拦住），
    因此不需要 `playwright install`。浏览器进程是你在用的那个，我们只开一个标签页并在用完关掉。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - 只在没装依赖时走到
        raise NumistaPageError(
            "取 Numista 页面需要 playwright（pip install -r requirements.txt）"
        ) from exc

    url = TYPE_PAGE_URL.format(type_id=type_id)
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise NumistaPageError(_cdp_hint(cdp_url, exc)) from exc

        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
            return await _wait_for_table(page, timeout_seconds)
        finally:
            await page.close()


async def _wait_for_catalogue(page, timeout_seconds: int) -> str:
    """等目录结果容器出现；`0 results found` 也算成功（空结果不是错误）。"""
    try:
        await page.wait_for_selector(_CAT_CONTAINER, timeout=timeout_seconds * 1000)
    except Exception:
        pass

    try:
        html = await page.content()
    except Exception:
        html = ""
    if "results found" in html:
        return html
    hint = (
        "；页面停在 Cloudflare 校验页（换一个「非自动化」启动的 Chrome 再试）"
        if _CHALLENGE in html
        else ""
    )
    raise NumistaPageError(f"等目录页结果超时 {timeout_seconds}s{hint}")


async def fetch_catalogue_page(
    config: Config, *, page_no: int = 1, cdp_url: str | None = None
) -> str:
    """按配置里的筛选条件打开目录列表页，返回渲染后的 HTML。

    同样只走 `connect_over_cdp`（见 `fetch_type_page` 的站点事实）。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - 只在没装依赖时走到
        raise NumistaPageError(
            "取 Numista 页面需要 playwright（pip install -r requirements.txt）"
        ) from exc

    url = catalogue_url(config.coins.collection.explore_catalogue, page=page_no)
    timeout_seconds = config.coins.numista.timeout_seconds
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.connect_over_cdp(
                cdp_url or config.coins.numista.cdp_url
            )
        except Exception as exc:
            raise NumistaPageError(
                _cdp_hint(cdp_url or config.coins.numista.cdp_url, exc)
            ) from exc

        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
            return await _wait_for_catalogue(page, timeout_seconds)
        finally:
            await page.close()


async def load_catalogue_index(
    config: Config, *, cdp_url: str | None = None
) -> CataloguePage:
    """按配置筛选并翻页取完目录结果（每页条数见 `explore_catalogue.per_page`）。"""
    items: list[CatalogueItem] = []
    total = 0
    page_no = 1
    while page_no <= 20:  # 防止筛选条件被改得过宽时无限翻页
        parsed = parse_catalogue_index(
            await fetch_catalogue_page(config, page_no=page_no, cdp_url=cdp_url)
        )
        total = parsed.total
        items.extend(parsed.items)
        if not parsed.items or len(items) >= total:
            break
        page_no += 1
    logger.info("Numista 目录：筛选后 %d 条，取到 %d 条", total, len(items))
    return CataloguePage(total=total, items=items)


# ---------------------------------------------------------------- 采集编排（落库）


def _values_row(
    row: IssueValueInfo, series_id: int, currency: str | None, fetched_at: str
) -> CoinIssueValues:
    return CoinIssueValues(
        id=None,
        numista_id=row.numista_issue_id,
        series_numista_id=series_id,
        currency=currency,
        value_g=row.value_g,
        value_vg=row.value_vg,
        value_f=row.value_f,
        value_vf=row.value_vf,
        value_xf=row.value_xf,
        value_au=row.value_au,
        value_unc=row.value_unc,
        frequency=row.frequency,
        fetched_at=fetched_at,
    )


async def collect_issue_values(
    config: Config, *, series_id: int, cdp_url: str | None = None
) -> TypePageInfo:
    """取某系列的类型页 → 解析 → 逐行落库（按单版编号幂等）。返回解析结果。

    一次页面加载就拿到**整个系列**的价格与 Frequency（不耗 Numista 接口额度）。
    """
    html = await fetch_type_page(
        cdp_url or config.coins.numista.cdp_url,
        series_id,
        timeout_seconds=config.coins.numista.timeout_seconds,
    )
    info = parse_type_page(html)
    fetched_at = repository.utcnow()
    for row in info.rows:
        repository.upsert_coin_issue_values(_values_row(row, series_id, info.currency, fetched_at))
    logger.info(
        "Numista 估值/频率：系列 %s 入库 %d 条（币种 %s）",
        series_id, len(info.rows), info.currency or "未知",
    )
    return info


# ---------------------------------------------------------------- 图片本地缓存


# 站点事实（2026-10-15 实测）：图片都在 `en.numista.com/catalogue/photos/...`，
# 响应带 `cross-origin-resource-policy: same-origin` —— 浏览器**必然**拦截跨站子资源
# （`ERR_BLOCKED_BY_RESPONSE.NotSameOrigin`），页面里没法直接 `<img>` 外链；
# 裸 `httpx` / `curl` 抓又吃 Cloudflare 403。故只能借你自己的 Chrome（同一条 CDP 链路）
# 把图**下载到本地**，再由服务从本地读出来。
IMAGE_DIR = PROJECT_ROOT / "data" / "coin_images"
IMAGE_SIDES = ("obverse", "reverse")


def image_path(numista_id: int, side: str, url: str | None = None) -> Path:
    """本地缓存路径：`data/coin_images/{系列号}-{side}{原后缀}`（后缀取不到就 `.jpg`）。"""
    suffix = Path(url).suffix if url and Path(url).suffix else ".jpg"
    return IMAGE_DIR / f"{numista_id}-{side}{suffix}"


def cached_image(numista_id: int, side: str) -> Path | None:
    """已缓存的本地图片；没有则 `None`（页面据此决定渲染本地图还是给原图链接）。"""
    if not IMAGE_DIR.is_dir():
        return None
    for path in sorted(IMAGE_DIR.glob(f"{numista_id}-{side}.*")):
        if path.is_file():
            return path
    return None


async def _fetch_bytes_via_cdp(
    cdp_url: str, type_id: int, urls: dict[str, str], *, timeout_seconds: int
) -> dict[str, bytes]:
    """借 CDP 浏览器把若干 URL 取成字节。

    先打开**类型页**过一遍 Cloudflare 的 JS 校验（拿到 `cf_clearance` 等校验态），
    再逐个**用页面导航**到图片地址读字节。实测：`context.request.get()`（Playwright 自己的请求）
    会吃 403 —— 它带不过浏览器的校验态；只有真实导航才行。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - 只在没装依赖时走到
        raise NumistaPageError(
            "下载图片需要 playwright（pip install -r requirements.txt）"
        ) from exc

    payloads: dict[str, bytes] = {}
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise NumistaPageError(_cdp_hint(cdp_url, exc)) from exc

        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(
                TYPE_PAGE_URL.format(type_id=type_id),
                wait_until="domcontentloaded",
                timeout=timeout_seconds * 1000,
            )
            await _wait_for_table(page, timeout_seconds)
            for side, url in urls.items():
                response = await page.goto(url, wait_until="load", timeout=timeout_seconds * 1000)
                if response is None or not response.ok:
                    raise NumistaPageError(
                        f"{side} 图下载失败：HTTP {response.status if response else '无响应'}（{url}）"
                    )
                body = await response.body()
                if not body:
                    raise NumistaPageError(f"{side} 图拿到空内容（{url}）")
                payloads[side] = body
        finally:
            await page.close()
    return payloads


async def download_series_images(
    config: Config, *, numista_id: int, cdp_url: str | None = None
) -> dict[str, Path]:
    """把某系列（或候选）的正 / 反图下载到 `data/coin_images/`，返回 `{side: 本地路径}`。

    需要本机开着带调试端口的 Chrome（与 `collect_issue_values` 同一条链路）。
    本地没有系列时回退到候选池保存的图片地址（候选池扫描会自动调用）。
    """
    series = repository.get_coin_series(numista_id)
    if series is not None:
        pictures = (series.obverse_picture, series.reverse_picture)
    else:
        candidate = repository.get_coin_explore_candidate(numista_id)
        if candidate is None:
            raise NumistaPageError(
                f"本地没有系列或候选 {numista_id}；"
                "先用 coins-series / coins-issues / coins-explore 把它采回来"
            )
        pictures = (
            candidate.payload.get("obverse_picture"),
            candidate.payload.get("reverse_picture"),
        )
    wanted = {
        side: url
        for side, url in (("obverse", pictures[0]), ("reverse", pictures[1]))
        if url
    }
    if not wanted:
        raise NumistaPageError(f"{numista_id} 没有图片地址，无法下载")

    payloads = await _fetch_bytes_via_cdp(
        cdp_url or config.coins.numista.cdp_url,
        numista_id,
        wanted,
        timeout_seconds=config.coins.numista.timeout_seconds,
    )

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    saved: dict[str, Path] = {}
    for side, body in payloads.items():
        path = image_path(numista_id, side, wanted[side])
        # 后缀可能与旧缓存不同，先清掉同一 side 的旧文件，避免留两份
        for stale in IMAGE_DIR.glob(f"{numista_id}-{side}.*"):
            if stale != path:
                stale.unlink(missing_ok=True)
        path.write_bytes(body)
        saved[side] = path
    logger.info(
        "Numista 图片：系列/候选 %s 缓存 %s", numista_id, sorted(p.name for p in saved.values())
    )
    return saved

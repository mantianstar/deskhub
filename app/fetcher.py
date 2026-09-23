"""RSS 抓取管道：URL 规范化、抓取解析、失败分类、入库（plan §7）。

不依赖 FastAPI、不感知请求上下文，因此 CLI 与调度器复用同一条管道。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import httpx

from app import repository
from app.config import Config
from app.models import FetchRun, FetchStatus, Item, Source

logger = logging.getLogger(__name__)

# URL 规范化：去掉这些参数（plan §7.2）
TRACKING_PARAMS = frozenset({"fbclid", "gclid", "ref", "source", "spm", "from"})
TRACKING_PREFIX = "utm_"
DEFAULT_PORTS = frozenset({"80", "443"})

SUMMARY_MAX_CHARS = 2000
# 单源整体超时 = httpx 超时 + 余量，兜住 feedparser 之外的意外挂起（plan §7.4）
OVERALL_TIMEOUT_MARGIN = 5

_REPEATED_SLASH = re.compile(r"/{2,}")
_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
# URL 路径里的发布日期，如 https://tech.meituan.com/2026/07/24/xxx.html
_URL_DATE = re.compile(r"/(\d{4})/(\d{1,2})/(\d{1,2})(?=/|$)")


# ---------------------------------------------------------------- URL 规范化


def canonicalize(url: str) -> str:
    """把 URL 归一成去重键的形式：

    scheme 统一 https、host 小写去 `www.`、去 fragment、去跟踪参数、
    路径去尾斜杠（根路径保留为 `/`）并合并重复 `/`、query 按 key 排序。

    无法归一的（空值、非 http(s)、没有 host）抛 `ValueError`，由调用方决定跳过。
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("URL 为空")

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("", "http", "https"):
        raise ValueError(f"只支持 http(s)，当前 scheme={scheme!r}")

    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"URL 缺少 host：{url!r}")
    if host.startswith("www."):
        host = host[4:]
    netloc = f"[{host}]" if ":" in host else host  # IPv6 字面量要带方括号
    port = parts.port
    if port is not None and str(port) not in DEFAULT_PORTS:
        netloc = f"{netloc}:{port}"

    # 根路径统一成 `/`，这样 `https://a.com` 与 `https://a.com/` 不会算成两条
    path = _REPEATED_SLASH.sub("/", parts.path)
    if not path:
        path = "/"
    elif path != "/" and path.endswith("/"):
        path = path[:-1]

    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking_param(key)
        )
    )
    return urlunsplit(("https", netloc, path, query, ""))


def url_hash(url: str) -> str:
    """规范化 URL 的 sha256，写入 `items.url_hash`（唯一约束即去重键）。"""
    return hashlib.sha256(canonicalize(url).encode("utf-8")).hexdigest()


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIX)


# ---------------------------------------------------------------- 文本清理


def clean_text(raw: str | None, *, limit: int | None = None) -> str | None:
    """去 HTML 标签与多余空白，可选截断；清理后为空返回 None。"""
    if not raw:
        return None
    text = _WHITESPACE.sub(" ", html.unescape(_HTML_TAG.sub(" ", raw))).strip()
    if not text:
        return None
    return text[:limit] if limit is not None else text


# ---------------------------------------------------------------- 解析


@dataclass(frozen=True)
class ParsedEntry:
    """feed 里解析出来的一条，尚未入库。"""

    title: str
    url: str
    url_hash: str
    published_at: str | None
    summary: str | None


@dataclass
class FetchOutcome:
    """单个源的抓取结果（CLI 打印与测试断言都用它）。"""

    source_id: int
    source_name: str
    status: str
    http_status: int | None
    parsed_count: int
    items_new: int
    error: str | None = None
    preview: tuple[ParsedEntry, ...] = field(default=())


def _published_at_from_url(link: str | None) -> str | None:
    """feed 完全没给时间时的兜底：从 URL 路径里的 `YYYY/MM/DD` 反解发布日期。

    美团技术团队的 feed 实测如此：`<item>` 里没有 `pubDate`/`dc:date`（channel 级那个是
    feed 生成时间，不是文章时间），但链接本身带日期 ——
    `https://tech.meituan.com/2026/07/24/LongCat-MineExplorer.html` 对应文章页上的
    `2026-07-24`（plan §12 第 14 条）。

    站点只给到「日」粒度，故取该日 00:00Z；页面按东八区显示成同日 08:00，日期不会漂。
    路径里的数字凑不出合法日期时返回 None —— 宁可不填，也不要编一个时间。
    """
    if not link:
        return None
    match = _URL_DATE.search(link)
    if match is None:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        stamp = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None
    return repository.to_utc_str(stamp)


def _entry_published_at(entry: dict, published_tz: ZoneInfo | None) -> str | None:
    """feedparser 的时间结构解析成 UTC 存储串；解析不出来返回 None。

    `published_tz` 不为空时表示「这个源的 feed 把**本地时间**标成了 GMT/UTC」
    （InfoQ 中文实测如此）：feedparser 会照字面把它当 UTC，我们要按声明时区重新解释一次，
    否则这条的 `published_at` 会整体偏掉一个时区（plan §12 第 12 条）。

    时间字段一个都没有时，再退一步从链接路径里反解日期（`_published_at_from_url`），
    这条兜底只对「feed 不给时间、但 URL 带日期」的源有意义，对其它源无影响。
    """
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if not value:
            continue
        try:
            stamp = datetime(*value[:6])
        except (TypeError, ValueError):
            continue
        stamp = stamp.replace(tzinfo=published_tz or timezone.utc)
        return repository.to_utc_str(stamp)
    return _published_at_from_url(entry.get("link"))


def parse_feed(
    raw: bytes,
    config: Config,
    *,
    published_tz: ZoneInfo | None = None,
) -> tuple[list[ParsedEntry], str | None]:
    """解析 feed 内容，返回（条目列表，解析错误）。

    条目按发布时间降序、取前 `max_items_per_source` 条；无链接或链接无法归一的条目跳过。
    """
    parsed_feed = feedparser.parse(raw)
    # 有 entries 就算解析成功：bozo 只是 feedparser 的「格式有瑕疵」标记
    if parsed_feed.bozo and not parsed_feed.entries:
        return [], f"feed 解析失败：{parsed_feed.get('bozo_exception')}"
    if not parsed_feed.entries:
        return [], None

    entries: list[ParsedEntry] = []
    skipped = 0
    for entry in parsed_feed.entries:
        link = (entry.get("link") or "").strip()
        if not link:
            skipped += 1
            continue
        try:
            hashed = url_hash(link)
        except ValueError as exc:
            skipped += 1
            logger.debug("跳过无法归一的链接：%s（%s）", link, exc)
            continue
        entries.append(
            ParsedEntry(
                title=clean_text(entry.get("title")) or "(无标题)",
                url=link,
                url_hash=hashed,
                published_at=_entry_published_at(entry, published_tz),
                summary=clean_text(
                    entry.get("summary") or entry.get("description"),
                    limit=SUMMARY_MAX_CHARS,
                ),
            )
        )
    if skipped:
        logger.warning("有 %d 条条目因缺少/非法链接被跳过", skipped)

    # ISO8601 字符串按字典序即时序；无发布时间的排到最后
    entries.sort(key=lambda item: item.published_at or "", reverse=True)
    return entries[: config.fetch.max_items_per_source], None


# ---------------------------------------------------------------- 单源抓取


async def _fetch_one(
    client: httpx.AsyncClient,
    source: Source,
    config: Config,
    *,
    dry_run: bool,
) -> FetchOutcome:
    started_at = repository.utcnow()
    http_status: int | None = None
    status = FetchStatus.OK
    error: str | None = None
    entries: list[ParsedEntry] = []
    # 源自己声明的时区修正（只有把本地时间标成 GMT 的源才需要，见 plan §12 第 12 条）
    source_config = config.source_for(source.url)
    published_tz = source_config.tz if source_config else None

    try:
        response = await asyncio.wait_for(
            client.get(source.url),
            timeout=config.fetch.timeout_seconds + OVERALL_TIMEOUT_MARGIN,
        )
        http_status = response.status_code
        if http_status >= 400:
            status = FetchStatus.HTTP_ERROR
            error = f"HTTP {http_status}"
        else:
            entries, parse_error = parse_feed(
                response.content, config, published_tz=published_tz
            )
            if parse_error:
                status = FetchStatus.PARSE_ERROR
                error = parse_error
            elif not entries:
                # 解析正常但 0 条：源是活的，不算故障（plan §7.3）
                status = FetchStatus.EMPTY
    except (httpx.TimeoutException, TimeoutError) as exc:
        status = FetchStatus.TIMEOUT
        error = f"{type(exc).__name__}: {exc}"
    except httpx.TransportError as exc:
        # 连接失败 / DNS / TLS 一律并入 timeout（plan §7.3）
        status = FetchStatus.TIMEOUT
        error = f"{type(exc).__name__}: {exc}"
    except httpx.HTTPError as exc:
        status = FetchStatus.HTTP_ERROR
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        # 未预期异常同样按单源隔离：落库 + 完整堆栈，不让一个源炸掉整轮（plan §1 铁律 2/4）
        logger.exception("源 %s 抓取出现未预期异常", source.name)
        status = FetchStatus.PARSE_ERROR
        error = f"{type(exc).__name__}: {exc}"

    run = FetchRun(
        id=None,
        source_id=source.id,
        started_at=started_at,
        finished_at=repository.utcnow(),
        status=status,
        http_status=http_status,
        items_new=0,
        error=error,
    )
    items = [
        Item(
            id=None,
            source_id=source.id,
            module=source.module,
            title=entry.title,
            url=entry.url,
            url_hash=entry.url_hash,
            published_at=entry.published_at,
            summary=entry.summary,
            fetched_at=started_at,
        )
        for entry in entries
    ]

    items_new = (
        0
        if dry_run
        # sqlite3 是同步库，丢到线程里跑，别阻塞事件循环（plan §6.3）
        else await asyncio.to_thread(repository.save_fetch_result, source.id, items, run)
    )

    logger.info(
        "抓取完成 source=%s status=%s http=%s 解析=%d 新增=%d%s",
        source.name,
        status,
        http_status,
        len(entries),
        items_new,
        f" error={error}" if error else "",
    )
    return FetchOutcome(
        source_id=source.id,
        source_name=source.name,
        status=status,
        http_status=http_status,
        parsed_count=len(entries),
        items_new=items_new,
        error=error,
        preview=tuple(entries) if dry_run else (),
    )


async def run_once(
    config: Config,
    *,
    source_id: int | None = None,
    dry_run: bool = False,
) -> list[FetchOutcome]:
    """抓取一轮，返回每个源的结果。

    - 默认只抓 `enabled=1` 的源；显式传 `source_id` 时不管启停（供单个源重试）。
    - 并发由 `fetch.max_concurrency` 控制（v1 串行）；`gather` 保证返回顺序与源清单一致。
    - `dry_run=True` 只解析、不写 `items` / `fetch_runs` / 源状态。
    """
    sources = repository.list_sources()
    if source_id is not None:
        sources = [source for source in sources if source.id == source_id]
        if not sources:
            raise ValueError(f"源不存在：id={source_id}")
    else:
        sources = [source for source in sources if source.enabled]

    semaphore = asyncio.Semaphore(config.fetch.max_concurrency)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(config.fetch.timeout_seconds),
        follow_redirects=True,
        headers={"User-Agent": config.fetch.user_agent},
    ) as client:

        async def fetch_guarded(source: Source) -> FetchOutcome:
            async with semaphore:
                return await _fetch_one(client, source, config, dry_run=dry_run)

        return list(await asyncio.gather(*(fetch_guarded(source) for source in sources)))

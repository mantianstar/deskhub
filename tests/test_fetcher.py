"""抓取管道测试：respx 模拟 HTTP，全程不访问真实网络（plan §13.1 用例 2-5）。

跑法：`.venv/bin/python -m pytest tests/test_fetcher.py -q -p no:cacheprovider`

样本来自 `tests/fixtures/*.xml`（M1-7 抓的真实 feed），解析逻辑改动靠它们回归。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from app import db, fetcher, repository
from app.config import (
    AppConfig,
    Config,
    DigestConfig,
    FetchConfig,
    LLMConfig,
    ModuleConfig,
    ScoringConfig,
    SourceConfig,
)
from app.models import FetchStatus

FIXTURES = Path(__file__).parent / "fixtures"

INFOQ_URL = "https://www.infoq.cn/feed"
OSCHINA_URL = "https://www.oschina.net/news/rss"
MEITUAN_URL = "https://tech.meituan.com/feed/"
BAD_URL = "https://bad.example.com/feed"
EMPTY_URL = "https://empty.example.com/feed"

INFOQ_XML = (FIXTURES / "infoq.xml").read_bytes()
OSCHINA_XML = (FIXTURES / "oschina.xml").read_bytes()
MEITUAN_XML = (FIXTURES / "meituan.xml").read_bytes()
# 合法 XML 但 0 条
EMPTY_XML = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b"<rss version=\"2.0\"><channel><title>empty</title></channel></rss>"
)
# 结构坏掉且解析不出条目
BROKEN_XML = b"<rss><channel><title>broken"


# ---------------------------------------------------------------- 脚手架


def _source(name: str, url: str, module: str = "agent", enabled: bool = True) -> SourceConfig:
    return SourceConfig(name=name, url=url, module=module, enabled=enabled)


def _config(
    *sources: SourceConfig,
    max_items_per_source: int = 30,
    timeout_seconds: int = 10,
) -> Config:
    """配置对象直接构造，不读真实 config.yaml（测试与运行环境解耦）。"""
    return Config(
        app=AppConfig(
            host="127.0.0.1",
            port=8765,
            timezone="Asia/Shanghai",
            db_path=Path("data/test.db"),
            log_path=Path("data/logs/test.log"),
        ),
        digest=DigestConfig(limit=8, lookback_hours=48),
        fetch=FetchConfig(
            timeout_seconds=timeout_seconds,
            user_agent="deskhub-test/1.0",
            max_items_per_source=max_items_per_source,
            max_concurrency=1,
        ),
        scoring=ScoringConfig(
            concurrency=3,
            max_summary_chars=1500,
            max_attempts=2,
            prompt_version="v1",
            temperature=0.2,
        ),
        llm=LLMConfig(
            base_url="https://api.example.com/v1",
            model="test-model",
            api_key_env="DESKHUB_TEST_KEY",
            api_key="",
        ),
        modules={
            "agent": ModuleConfig(key="agent", label="Agent 开发", focus="Agent 方向"),
            "bigdata": ModuleConfig(key="bigdata", label="大数据", focus="大数据方向"),
        },
        sources=sources,
    )


def _prepare(tmp_path: Path, *sources: SourceConfig, **kwargs) -> Config:
    """临时库 + 源清单同步，返回配置。"""
    db.init_db(tmp_path / "test.db")
    config = _config(*sources, **kwargs)
    repository.sync_sources_from_config(config.sources)
    return config


def _count(table: str) -> int:
    with db.connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _source_row(url: str) -> sqlite3.Row:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM sources WHERE url = ?", (url,)).fetchone()


def _outcome(outcomes: list[fetcher.FetchOutcome], name: str) -> fetcher.FetchOutcome:
    return next(outcome for outcome in outcomes if outcome.source_name == name)


# ---------------------------------------------------------------- 幂等


@pytest.mark.asyncio
async def test_same_feed_twice_only_inserts_once(tmp_path, respx_mock):
    """同一 feed 抓两次：第二次 items_new=0，items 总数不变。"""
    config = _prepare(tmp_path, _source("InfoQ 中文", INFOQ_URL))
    respx_mock.get(INFOQ_URL).mock(return_value=httpx.Response(200, content=INFOQ_XML))

    first = await fetcher.run_once(config)
    assert first[0].status == FetchStatus.OK
    assert first[0].parsed_count > 0
    assert first[0].items_new == first[0].parsed_count
    total_after_first = _count("items")

    second = await fetcher.run_once(config)
    assert second[0].items_new == 0
    assert _count("items") == total_after_first
    assert _count("fetch_runs") == 2
    # 条目全部已存在仍算 ok，不误报故障（plan §7.3）
    assert _source_row(INFOQ_URL)["fail_count"] == 0


# ---------------------------------------------------------------- 失败分类


@pytest.mark.asyncio
async def test_http_500_marks_failure_and_other_sources_survive(tmp_path, respx_mock):
    """一个源 500 只影响它自己：fail_count=1、last_ok_at 不变，其他源照常抓。"""
    config = _prepare(
        tmp_path,
        _source("坏源", BAD_URL),
        _source("InfoQ 中文", INFOQ_URL),
    )
    respx_mock.get(BAD_URL).mock(return_value=httpx.Response(500))
    respx_mock.get(INFOQ_URL).mock(return_value=httpx.Response(200, content=INFOQ_XML))

    outcomes = await fetcher.run_once(config)

    bad = _outcome(outcomes, "坏源")
    assert bad.status == FetchStatus.HTTP_ERROR
    assert bad.http_status == 500
    bad_row = _source_row(BAD_URL)
    assert bad_row["fail_count"] == 1
    assert bad_row["last_ok_at"] is None
    with db.connect() as conn:
        run = conn.execute(
            "SELECT * FROM fetch_runs WHERE source_id = ?", (bad_row["id"],)
        ).fetchone()
    assert run["status"] == FetchStatus.HTTP_ERROR
    assert run["http_status"] == 500
    assert run["error"] == "HTTP 500"
    assert run["finished_at"] is not None

    good = _outcome(outcomes, "InfoQ 中文")
    assert good.status == FetchStatus.OK
    assert good.items_new == good.parsed_count > 0


@pytest.mark.asyncio
async def test_timeout_is_classified_as_timeout(tmp_path, respx_mock):
    """连接超时 / 连不上都算 timeout，且 fail_count +1。"""
    config = _prepare(tmp_path, _source("超时源", BAD_URL))
    respx_mock.get(BAD_URL).mock(side_effect=httpx.ConnectTimeout("connect timed out"))

    outcomes = await fetcher.run_once(config)

    assert outcomes[0].status == FetchStatus.TIMEOUT
    assert outcomes[0].http_status is None
    assert outcomes[0].error
    assert _source_row(BAD_URL)["fail_count"] == 1


@pytest.mark.asyncio
async def test_broken_feed_is_parse_error(tmp_path, respx_mock):
    config = _prepare(tmp_path, _source("坏 XML", BAD_URL))
    respx_mock.get(BAD_URL).mock(return_value=httpx.Response(200, content=BROKEN_XML))

    outcomes = await fetcher.run_once(config)

    assert outcomes[0].status == FetchStatus.PARSE_ERROR
    assert _source_row(BAD_URL)["fail_count"] == 1


@pytest.mark.asyncio
async def test_empty_feed_keeps_fail_count_and_touches_last_ok_at(tmp_path, respx_mock):
    """解析正常但 0 条：源还是活的（更新 last_ok_at），但不该洗掉历史失败计数。"""
    config = _prepare(tmp_path, _source("空源", EMPTY_URL))
    respx_mock.get(EMPTY_URL).mock(return_value=httpx.Response(200, content=EMPTY_XML))
    with db.connect() as conn, conn:
        conn.execute("UPDATE sources SET fail_count = 2 WHERE url = ?", (EMPTY_URL,))

    outcomes = await fetcher.run_once(config)

    assert outcomes[0].status == FetchStatus.EMPTY
    assert outcomes[0].parsed_count == 0
    row = _source_row(EMPTY_URL)
    assert row["fail_count"] == 2
    assert row["last_ok_at"] is not None


@pytest.mark.asyncio
async def test_three_failures_then_success_resets_fail_count(tmp_path, respx_mock):
    """连续 3 次失败后成功：fail_count 归零、不再标红。"""
    config = _prepare(tmp_path, _source("时好时坏", INFOQ_URL))
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(503)
        return httpx.Response(200, content=INFOQ_XML)

    respx_mock.get(INFOQ_URL).mock(side_effect=responder)

    for _ in range(3):
        await fetcher.run_once(config)
    assert _source_row(INFOQ_URL)["fail_count"] == 3
    assert repository.count_failing_sources() == 1  # 达到标红阈值

    outcomes = await fetcher.run_once(config)

    assert outcomes[0].status == FetchStatus.OK
    assert _source_row(INFOQ_URL)["fail_count"] == 0
    assert _source_row(INFOQ_URL)["last_ok_at"] is not None
    assert repository.count_failing_sources() == 0


# ---------------------------------------------------------------- 解析细节


@pytest.mark.asyncio
async def test_takes_newest_items_up_to_cap(tmp_path, respx_mock):
    """按发布时间降序取前 max_items_per_source 条。"""
    respx_mock.get(OSCHINA_URL).mock(return_value=httpx.Response(200, content=OSCHINA_XML))

    full = _prepare(
        tmp_path, _source("开源中国资讯", OSCHINA_URL, module="bigdata"), max_items_per_source=50
    )
    everything = (await fetcher.run_once(full, dry_run=True))[0]
    assert everything.parsed_count == 50

    capped = _prepare(
        tmp_path, _source("开源中国资讯", OSCHINA_URL, module="bigdata"), max_items_per_source=5
    )
    subset = (await fetcher.run_once(capped, dry_run=True))[0]
    assert subset.parsed_count == 5
    assert [entry.url_hash for entry in subset.preview] == [
        entry.url_hash for entry in everything.preview[:5]
    ]
    times = [entry.published_at or "" for entry in subset.preview]
    assert times == sorted(times, reverse=True)


@pytest.mark.asyncio
async def test_entry_without_link_is_skipped(tmp_path, respx_mock):
    """没有 link 的条目跳过；有 link 的照常入库，标题/摘要做清理。"""
    xml = (
        b'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
        b"<title>t</title>"
        b"<item><title>no link</title><description>x</description></item>"
        b"<item><title>A &amp; B</title><link>https://example.com/a?utm_source=rss</link>"
        b"<pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate>"
        b"<description>&lt;p&gt;hello   world&lt;/p&gt;</description></item>"
        b"</channel></rss>"
    )
    config = _prepare(tmp_path, _source("手写源", BAD_URL))
    respx_mock.get(BAD_URL).mock(return_value=httpx.Response(200, content=xml))

    outcomes = await fetcher.run_once(config)

    assert outcomes[0].parsed_count == 1
    assert outcomes[0].items_new == 1
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM items").fetchone()
    assert row["title"] == "A & B"
    assert row["url"] == "https://example.com/a?utm_source=rss"  # 原始链接保留，便于跳转
    assert fetcher.canonicalize(row["url"]) == "https://example.com/a"
    assert row["summary"] == "hello world"
    assert row["published_at"] == "2026-09-21T10:00:00Z"
    assert row["module"] == "agent"  # 继承自源


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(tmp_path, respx_mock):
    """--dry-run 只解析：items / fetch_runs / 源状态都不动。"""
    config = _prepare(tmp_path, _source("InfoQ 中文", INFOQ_URL))
    respx_mock.get(INFOQ_URL).mock(return_value=httpx.Response(200, content=INFOQ_XML))

    outcomes = await fetcher.run_once(config, dry_run=True)

    assert outcomes[0].parsed_count > 0
    assert outcomes[0].preview
    assert _count("items") == 0
    assert _count("fetch_runs") == 0
    row = _source_row(INFOQ_URL)
    assert row["last_ok_at"] is None
    assert row["fail_count"] == 0


# ---------------------------------------------------------------- 源级时区修正


# 源把北京时间当成 GMT 标（InfoQ 中文实测就是这个写法）
def _gmt_feed(link: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
        "<title>t</title>"
        f"<item><title>本地时间标成 GMT</title><link>{link}</link>"
        "<pubDate>Wed, 23 Sep 2026 09:26:00 GMT</pubDate>"
        "<description>x</description></item>"
        "</channel></rss>"
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_source_timezone_corrects_mislabelled_gmt(tmp_path, respx_mock):
    """源声明 published_tz 后，feed 里标成 GMT 的本地时间按声明时区重新解释（−8h）。"""
    config = _prepare(
        tmp_path,
        SourceConfig(
            name="InfoQ 中文", url=BAD_URL, module="agent", enabled=True, published_tz="Asia/Shanghai"
        ),
    )
    respx_mock.get(BAD_URL).mock(return_value=httpx.Response(200, content=_gmt_feed("https://example.com/gmt")))

    await fetcher.run_once(config)

    with db.connect() as conn:
        assert conn.execute("SELECT published_at FROM items").fetchone()[0] == "2026-09-23T01:26:00Z"


@pytest.mark.asyncio
async def test_without_source_timezone_gmt_is_taken_literally(tmp_path, respx_mock):
    """没声明 published_tz 的源维持原行为：feed 标 GMT 就按 UTC 存（掘金、博客园都靠这条）。"""
    config = _prepare(tmp_path, _source("手写源", BAD_URL))
    respx_mock.get(BAD_URL).mock(return_value=httpx.Response(200, content=_gmt_feed("https://example.com/gmt")))

    await fetcher.run_once(config)

    with db.connect() as conn:
        assert conn.execute("SELECT published_at FROM items").fetchone()[0] == "2026-09-23T09:26:00Z"


@pytest.mark.asyncio
async def test_source_timezone_does_not_shift_other_sources(tmp_path, respx_mock):
    """时区修正按 url 生效，不会串到别的源上。"""
    config = _prepare(
        tmp_path,
        SourceConfig(
            name="被修正的源", url=BAD_URL, module="agent", enabled=True, published_tz="Asia/Shanghai"
        ),
        _source("正常源", EMPTY_URL),
    )
    respx_mock.get(BAD_URL).mock(return_value=httpx.Response(200, content=_gmt_feed("https://example.com/fixed")))
    respx_mock.get(EMPTY_URL).mock(return_value=httpx.Response(200, content=_gmt_feed("https://example.com/normal")))

    await fetcher.run_once(config)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.name, i.published_at FROM items i JOIN sources s ON s.id = i.source_id "
            "ORDER BY s.name"
        ).fetchall()
    assert [(row["name"], row["published_at"]) for row in rows] == [
        ("正常源", "2026-09-23T09:26:00Z"),
        ("被修正的源", "2026-09-23T01:26:00Z"),
    ]


# ---------------------------------------------------------------- 无时间源的日期兜底


def test_url_date_parsing_boundaries():
    """路径里的数字凑不出合法日期就返回 None —— 宁可不填，也不编时间。"""
    assert (
        fetcher._published_at_from_url("https://tech.meituan.com/2026/7/4/a.html")
        == "2026-07-04T00:00:00Z"
    )
    assert fetcher._published_at_from_url("https://tech.meituan.com/2026/13/45/a.html") is None
    assert fetcher._published_at_from_url("https://tech.meituan.com/2026/02/30/a.html") is None
    assert fetcher._published_at_from_url("https://example.com/2026/09/12345/a.html") is None
    assert fetcher._published_at_from_url("https://example.com/posts/hello.html") is None
    assert fetcher._published_at_from_url("") is None
    assert fetcher._published_at_from_url(None) is None


@pytest.mark.asyncio
async def test_meituan_feed_gets_published_at_from_url(tmp_path, respx_mock):
    """美团 feed 的 item 没有时间，从 URL 路径反解（plan §12 第 14 条）。"""
    config = _prepare(tmp_path, _source("美团技术团队", MEITUAN_URL, module="bigdata"))
    respx_mock.get(MEITUAN_URL).mock(return_value=httpx.Response(200, content=MEITUAN_XML))

    outcomes = await fetcher.run_once(config)

    assert outcomes[0].parsed_count == 10
    assert outcomes[0].items_new == 10

    with db.connect() as conn:
        rows = conn.execute("SELECT url, published_at FROM items").fetchall()
    assert len(rows) == 10
    assert all(row["published_at"] for row in rows)  # 10 条全部有发布时间
    assert max(row["published_at"] for row in rows) == "2026-09-22T00:00:00Z"  # 样本里最新的一篇
    # 抽查：URL 里的日期就是入库的发布日期（文章页上显示的就是这个）
    july = next(row for row in rows if "/2026/07/24/" in row["url"])
    assert july["published_at"] == "2026-07-24T00:00:00Z"


@pytest.mark.asyncio
async def test_feed_time_wins_over_url_date(tmp_path, respx_mock):
    """feed 给了时间就用 feed 的，URL 里的日期只作兜底。"""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>t</title>'
        "<item><title>有 pubDate</title><link>https://example.com/2019/01/01/old.html</link>"
        "<pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate><description>x</description></item>"
        "</channel></rss>"
    ).encode("utf-8")
    config = _prepare(tmp_path, _source("手写源", BAD_URL))
    respx_mock.get(BAD_URL).mock(return_value=httpx.Response(200, content=xml))

    await fetcher.run_once(config)

    with db.connect() as conn:
        assert (
            conn.execute("SELECT published_at FROM items").fetchone()[0] == "2026-09-21T10:00:00Z"
        )


# ---------------------------------------------------------------- 源筛选


@pytest.mark.asyncio
async def test_disabled_source_is_not_fetched(tmp_path, respx_mock):
    config = _prepare(
        tmp_path,
        _source("停用源", BAD_URL, enabled=False),
        _source("InfoQ 中文", INFOQ_URL),
    )
    respx_mock.get(INFOQ_URL).mock(return_value=httpx.Response(200, content=INFOQ_XML))

    outcomes = await fetcher.run_once(config)

    assert [outcome.source_name for outcome in outcomes] == ["InfoQ 中文"]


@pytest.mark.asyncio
async def test_explicit_source_id_fetches_even_if_disabled(tmp_path, respx_mock):
    """单个源重试要能绕开启停（M5 的「立即重试」按钮靠它）。"""
    config = _prepare(tmp_path, _source("停用源", INFOQ_URL, enabled=False))
    respx_mock.get(INFOQ_URL).mock(return_value=httpx.Response(200, content=INFOQ_XML))
    source_id = _source_row(INFOQ_URL)["id"]

    outcomes = await fetcher.run_once(config, source_id=source_id)

    assert len(outcomes) == 1
    assert outcomes[0].items_new > 0


@pytest.mark.asyncio
async def test_unknown_source_id_raises(tmp_path):
    config = _prepare(tmp_path, _source("InfoQ 中文", INFOQ_URL))

    with pytest.raises(ValueError):
        await fetcher.run_once(config, source_id=999, dry_run=True)

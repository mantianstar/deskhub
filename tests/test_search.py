"""历史搜索：关键词 / 时间范围 / 入口筛选 + 分页 + 坏参数降级（M4-1 ~ M4-3）。

运行方式：`.venv/bin/python -m pytest tests/test_search.py -q`
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import config, db, fetcher, repository
from app.config import SourceConfig
from app.main import app as fastapi_app
from app.models import Item

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAY_START, DAY_END = "2026-09-21T16:00:00Z", "2026-09-22T16:00:00Z"  # 上海 2026-09-22 全天


@pytest.fixture()
def temp_db(tmp_path):
    """每个用例一个独立库文件，避免用例间互相污染。"""
    db.init_db(tmp_path / "test.db")
    return tmp_path


@pytest.fixture()
def web(tmp_path, monkeypatch):
    """走真实 lifespan（建库 + 同步源清单），库文件落在临时目录。"""
    monkeypatch.setenv("DESKHUB_DB_PATH", str(tmp_path / "web.db"))
    config.reset()
    with TestClient(fastapi_app) as client:
        yield client
    config.reset()


def _seed_source() -> int:
    repository.sync_sources_from_config(
        (
            SourceConfig(
                name="InfoQ 中文", url="https://www.infoq.cn/feed", module="agent", enabled=True
            ),
        )
    )
    return repository.list_sources()[0].id


def _add_item(
    source_id: int,
    title: str,
    *,
    url: str,
    fetched_at: str,
    published_at: str | None = None,
    summary: str = "摘要",
    module: str = "agent",
) -> int:
    item_id = repository.insert_item_if_new(
        Item(
            id=None,
            source_id=source_id,
            module=module,
            title=title,
            url=url,
            url_hash=fetcher.url_hash(url),
            published_at=published_at,
            summary=summary,
            fetched_at=fetched_at,
        )
    )
    assert item_id is not None
    return item_id


# ---------------------------------------------------------------- 查询层


def test_search_matches_title_and_summary(temp_db):
    """关键词命中标题**或**摘要（plan §7 的「标题 + 摘要 LIKE」）。"""
    source_id = _seed_source()
    by_title = _add_item(
        source_id,
        "LangGraph 多智能体实战",
        url="https://example.com/a",
        fetched_at="2026-09-22T02:00:00Z",
        summary="与关键词无关的摘要",
    )
    by_summary = _add_item(
        source_id,
        "本周技术周报",
        url="https://example.com/b",
        fetched_at="2026-09-22T03:00:00Z",
        summary="聊了聊 Spark 调优的几个坑",
    )

    hits, total = repository.search_items(q="LangGraph")
    assert (total, [item.item_id for item in hits]) == (1, [by_title])

    hits, total = repository.search_items(q="Spark")
    assert (total, [item.item_id for item in hits]) == (1, [by_summary])

    # 摘要有 NULL 的条目不会因 OR 里带 NULL 而漏掉标题命中
    null_summary = _add_item(
        source_id,
        "Flink 状态后端",
        url="https://example.com/c",
        fetched_at="2026-09-22T04:00:00Z",
        summary="",
    )
    hits, total = repository.search_items(q="Flink")
    assert (total, [item.item_id for item in hits]) == (1, [null_summary])

    assert repository.search_items(q="完全不存在的词") == ([], 0)


def test_search_filters_by_published_time_not_fetched_time(temp_db):
    """时间范围筛的是「发布时间」，业务时区折算、左闭右开（plan §12 第 13 条）。

    所有条目的 `fetched_at` 都是同一时刻（今天抓的），只有 `published_at` 各不相同 ——
    命中数只随发布时间变，就证明筛选没有看抓取时间。
    """
    source_id = _seed_source()
    fetched = "2026-09-23T01:00:00Z"  # 全部是 09-23（北京 09:00）抓的
    in_window_agent = _add_item(
        source_id,
        "窗口内 agent",
        url="https://example.com/in-agent",
        fetched_at=fetched,
        published_at="2026-09-22T02:00:00Z",  # 北京 09-22 10:00
    )
    in_window_bigdata = _add_item(
        source_id,
        "窗口内 bigdata",
        url="https://example.com/in-bigdata",
        fetched_at=fetched,
        published_at="2026-09-22T10:00:00Z",  # 北京 09-22 18:00
        module="bigdata",
    )
    out_window_old = _add_item(
        source_id,
        "窗口外的旧发布",
        url="https://example.com/out-old",
        fetched_at=fetched,
        published_at="2026-09-21T02:00:00Z",  # 北京 09-21 10:00
    )
    boundary = _add_item(
        source_id,
        "窗口终点那一刻",
        url="https://example.com/boundary",
        fetched_at=fetched,
        published_at=DAY_END,  # 北京 09-23 00:00，属于次日
    )
    no_published = _add_item(
        source_id,
        "美团：源没有给发布时间",
        url="https://example.com/no-published",
        fetched_at=fetched,
    )

    assert repository.day_bounds_utc(SHANGHAI, date(2026, 9, 22)) == (DAY_START, DAY_END)

    # 左闭右开，且按发布时间降序
    hits, total = repository.search_items(start_utc=DAY_START, end_utc=DAY_END)
    assert (total, [item.item_id for item in hits]) == (2, [in_window_bigdata, in_window_agent])

    hits, total = repository.search_items(module="agent", start_utc=DAY_START, end_utc=DAY_END)
    assert (total, [item.item_id for item in hits]) == (1, [in_window_agent])

    # 没有发布时间的条目：设了时间范围搜不到（没有可筛的时间），不设时间范围照常搜得到
    hits, total = repository.search_items(module="agent")
    assert {item.item_id for item in hits} == {
        in_window_agent,
        out_window_old,
        boundary,
        no_published,
    }
    assert repository.search_items(q="没有给发布时间")[1] == 1


def test_search_paginates_by_published_time(temp_db):
    """按「发布时间」倒序分页，返回命中总数（不是本页条数）。

    发布时间顺序故意与入库顺序（id）不一致，这样能区分「按发布时间排」和「按入库时间/ id 排」。
    """
    source_id = _seed_source()
    hours = {1: 3, 2: 1, 3: 5, 4: 2, 5: 4}
    ids = {
        index: _add_item(
            source_id,
            f"条目 {index}",
            url=f"https://example.com/p{index}",
            fetched_at="2026-09-23T01:00:00Z",  # 抓取时间全部相同 → 排序只能由发布时间决定
            published_at=f"2026-09-22T0{hour}:00:00Z",
        )
        for index, hour in hours.items()
    }
    by_published_desc = [ids[3], ids[5], ids[1], ids[4], ids[2]]

    first, total = repository.search_items(page=1, page_size=2)
    assert total == 5
    assert [item.item_id for item in first] == by_published_desc[:2]

    third, _ = repository.search_items(page=3, page_size=2)
    assert [item.item_id for item in third] == by_published_desc[4:]

    beyond, total = repository.search_items(page=99, page_size=2)
    assert (total, beyond) == (5, [])

    # 页码/页大小传坏值时自行兜底（route 层也会再兜一次）
    assert repository.search_items(page=0, page_size=0)[1] == 5


# ---------------------------------------------------------------- 路由与页面


def test_search_page_hits_keyword_and_grades_score(web):
    """页面能按关键词命中当日抓到的条目，外链走 /go/{id}（M4-4）。"""
    source_id = repository.list_sources(only_enabled=True)[0].id
    item_id = _add_item(
        source_id,
        "使用 DuckDB 分析 CSV 文件",
        url="https://example.com/duckdb",
        fetched_at=repository.utcnow(),
    )
    repository.upsert_score(item_id, 62, "讲的是 DuckDB 的用法，与大数据栈相关", "fake", "v1")

    page = web.get("/search", params={"q": "DuckDB"})
    assert page.status_code == 200
    assert "使用 DuckDB 分析 CSV 文件" in page.text
    assert f"/go/{item_id}" in page.text
    assert "共 1 条" in page.text
    # 命中标题的摘要里没有关键词，说明标题确实参与了匹配
    assert "讲的是 DuckDB 的用法" in page.text

    empty = web.get("/search", params={"q": "不存在的关键词"})
    assert empty.status_code == 200
    assert "没有匹配的条目" in empty.text


def test_search_page_filters_by_published_date(web):
    """页面上的时间范围筛的是「发布时间」，不是抓取时间（plan §12 第 13 条）。

    两条都是今天抓的，只有发布时间不同 —— 若还是按抓取时间筛，两条都会出现。
    """
    source_id = repository.list_sources(only_enabled=True)[0].id
    _add_item(
        source_id,
        "今天发布的条目",
        url="https://example.com/pub-today",
        fetched_at=repository.utcnow(),
        published_at="2026-09-23T02:00:00Z",  # 北京 09-23 10:00
    )
    _add_item(
        source_id,
        "昨天发布的条目",
        url="https://example.com/pub-yesterday",
        fetched_at=repository.utcnow(),
        published_at="2026-09-22T02:00:00Z",  # 北京 09-22 10:00
    )

    page = web.get("/search", params={"start": "2026-09-23", "end": "2026-09-23"})
    assert page.status_code == 200
    assert "今天发布的条目" in page.text
    assert "昨天发布的条目" not in page.text
    assert "共 1 条" in page.text

    both = web.get("/search", params={"start": "2026-09-22", "end": "2026-09-23"})
    assert "今天发布的条目" in both.text and "昨天发布的条目" in both.text


def test_search_page_keeps_filters_in_pager_url(web):
    """翻页链接带上全部筛选条件：URL 可分享、可回退（M4-3）。"""
    source_id = repository.list_sources(only_enabled=True)[0].id
    for index in range(3):
        _add_item(
            source_id,
            f"DuckDB 笔记 {index}",
            url=f"https://example.com/duckdb-{index}",
            fetched_at=repository.utcnow(),
            published_at=f"2026-09-22T0{index}:00:00Z",
        )

    page = web.get("/search", params={"q": "DuckDB", "page_size": 2})
    assert page.status_code == 200
    assert "第 1 / 2 页" in page.text
    assert "q=DuckDB&amp;page=2&amp;page_size=2" in page.text  # 下一页链接保留关键词与页大小

    second = web.get("/search", params={"q": "DuckDB", "page_size": 2, "page": 2})
    assert "第 2 / 2 页" in second.text
    assert "q=DuckDB&amp;page=1&amp;page_size=2" in second.text  # 回退链接同样保留筛选
    assert "DuckDB 笔记 0" in second.text


def test_search_page_tolerates_bad_params(web):
    """非法页码 / 日期 / module 一律降级，不 500（M4-2）。"""
    source_id = repository.list_sources(only_enabled=True)[0].id
    _add_item(
        source_id,
        "能被搜到的条目",
        url="https://example.com/any",
        fetched_at=repository.utcnow(),
    )

    for query in (
        {"page": "abc"},
        {"page": "-3"},
        {"page": "0"},
        {"page": "9999"},
        {"page_size": "0"},
        {"page_size": "99999"},
        {"start": "2026-13-99"},
        {"end": "not-a-date"},
        {"module": "unknown"},
        {"q": "   "},
    ):
        response = web.get("/search", params=query)
        assert response.status_code == 200, query

    # 未知 module 当作「全部」而不是「无结果」
    assert "能被搜到的条目" in web.get("/search", params={"module": "unknown"}).text
    # 页码超出范围时落到最后一页
    assert "第 1 / 1 页" in web.get("/search", params={"page": "9999"}).text


def test_search_page_renders_advanced_filters_expanded_when_active(web):
    """筛选生效时高级区默认展开，否则收起（由原生 JS 切换）。"""
    collapsed = web.get("/search")
    assert collapsed.status_code == 200
    assert "id=\"advanced-filters\" hidden" in collapsed.text

    expanded = web.get("/search", params={"module": "agent", "start": "2026-09-01"})
    assert expanded.status_code == 200
    assert "id=\"advanced-filters\"" in expanded.text
    assert "id=\"advanced-filters\" hidden" not in expanded.text
    assert 'value="2026-09-01"' in expanded.text
    assert 'value="agent" selected' in expanded.text

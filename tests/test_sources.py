"""入口视图 / 源管理 / 占位页：M5 外壳（plan §10、§13.2 第 5 条）。

运行方式：`.venv/bin/python -m pytest tests/test_sources.py -q`
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, fetcher, repository
from app.main import app as fastapi_app
from app.models import FetchRun, FetchStatus, Item
from app.routers import format_local_time

FIXTURES = Path(__file__).parent / "fixtures"
INFOQ_URL = "https://www.infoq.cn/feed"
INFOQ_XML = (FIXTURES / "infoq.xml").read_bytes()


@pytest.fixture()
def web(tmp_path, monkeypatch, no_scheduler):
    """走真实 lifespan（建库 + 同步 config/sources.yaml 的 6 个源），库落在临时目录、调度与补拉走空壳。"""
    monkeypatch.setenv("DESKHUB_DB_PATH", str(tmp_path / "web.db"))
    config.reset()
    with TestClient(fastapi_app) as client:
        yield client
    config.reset()


# ---------------------------------------------------------------- 脚手架


def _get_source(source_id: int):
    return next(source for source in repository.list_sources() if source.id == source_id)


def _add_item(source_id: int, title: str, *, url: str, module: str) -> int:
    item_id = repository.insert_item_if_new(
        Item(
            id=None,
            source_id=source_id,
            module=module,
            title=title,
            url=url,
            url_hash=fetcher.url_hash(url),
            published_at=None,
            summary="摘要",
            fetched_at=repository.utcnow(),
        )
    )
    assert item_id is not None
    return item_id


def _record(source_id: int, status: str, *, error: str | None = None, http_status: int | None = None):
    """直接落一条抓取结果，用来构造「源挂了 / 源正常」的状态，不碰真实网络。"""
    now = repository.utcnow()
    run = FetchRun(
        id=None,
        source_id=source_id,
        started_at=now,
        finished_at=now,
        status=status,
        http_status=http_status,
        items_new=0,
        error=error,
    )
    repository.save_fetch_result(source_id, [], run)


# ---------------------------------------------------------------- 入口视图 M5-1


def test_module_page_shows_only_that_module_and_404_for_unknown(web):
    """两个入口各自只出现本 module 条目；非法 module 返回 404（M5-1）。"""
    sources = repository.list_sources(only_enabled=True)
    agent_src = next(source for source in sources if source.module == "agent")
    _add_item(agent_src.id, "agent 独有的条目", url="https://example.com/a", module="agent")
    _add_item(agent_src.id, "bigdata 独有的条目", url="https://example.com/b", module="bigdata")

    agent_page = web.get("/m/agent")
    assert agent_page.status_code == 200
    assert "agent 独有的条目" in agent_page.text
    assert "bigdata 独有的条目" not in agent_page.text

    bigdata_page = web.get("/m/bigdata")
    assert bigdata_page.status_code == 200
    assert "bigdata 独有的条目" in bigdata_page.text
    assert "agent 独有的条目" not in bigdata_page.text

    assert web.get("/m/unknown").status_code == 404


# ---------------------------------------------------------------- 源管理 M5-2 / M5-6


def test_sources_page_marks_only_failing_source_red(web):
    """连续失败 >=3 的源标红并带 error/http_status 的 tooltip；其他源不受牵连（M5-2、M5-6）。"""
    bad, good = repository.list_sources(only_enabled=True)[:2]
    for _ in range(3):
        _record(bad.id, FetchStatus.TIMEOUT, error="ConnectTimeout: connect timed out")
    _record(good.id, FetchStatus.OK, http_status=200)

    page = web.get("/sources")
    assert page.status_code == 200
    # 只有坏源那一行被标红
    assert page.text.count("row-failing") == 1
    assert "ConnectTimeout: connect timed out" in page.text  # tooltip 带最后一次 error
    good_after = _get_source(good.id)
    assert good_after.fail_count == 0
    assert good_after.last_ok_at is not None
    # 好源显示的是成功时间而不是「从未成功」
    assert format_local_time(good_after.last_ok_at) in page.text


def test_sources_page_lists_all_sources(web):
    """源列表把停用源也列出来（源清单是配置，停用不等于删除，plan §16）。"""
    page = web.get("/sources")
    assert page.status_code == 200
    for source in repository.list_sources():
        assert source.name in page.text


# ---------------------------------------------------------------- 源操作 M5-3


def test_toggle_source_flips_enabled_and_404_for_unknown(web):
    source = repository.list_sources()[0]
    assert source.enabled is True

    response = web.post(f"/sources/{source.id}/toggle", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/sources"
    assert _get_source(source.id).enabled is False

    web.post(f"/sources/{source.id}/toggle", follow_redirects=False)
    assert _get_source(source.id).enabled is True

    assert web.post("/sources/999999/toggle", follow_redirects=False).status_code == 404


def test_fetch_source_redirects_and_clears_fail_count(web, respx_mock):
    """挂掉的源可单独重试：成功 302、fail_count 归零、红点消失（M5-3）。"""
    source = next(item for item in repository.list_sources() if item.url == INFOQ_URL)
    _record(source.id, FetchStatus.HTTP_ERROR, error="HTTP 500", http_status=500)
    _record(source.id, FetchStatus.HTTP_ERROR, error="HTTP 500", http_status=500)
    assert _get_source(source.id).fail_count == 2
    respx_mock.get(INFOQ_URL).mock(return_value=httpx.Response(200, content=INFOQ_XML))

    response = web.post(f"/sources/{source.id}/fetch", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/sources"
    refreshed = _get_source(source.id)
    assert refreshed.fail_count == 0
    assert refreshed.last_ok_at is not None


def test_fetch_unknown_source_returns_404(web):
    assert web.post("/sources/999999/fetch", follow_redirects=False).status_code == 404


# ---------------------------------------------------------------- 占位页与导航 M5-4 / M5-5


def test_placeholder_pages_show_not_enabled(web):
    for path, title in (("/funds", "基金监控"), ("/coins", "铜币拍卖")):
        page = web.get(path)
        assert page.status_code == 200
        assert title in page.text
        assert "未启用" in page.text


def test_all_nav_links_resolve(web):
    """导航栏 7 个入口点了都不 404（M5-5）。"""
    links = ["/", "/m/agent", "/m/bigdata", "/search", "/sources", "/funds", "/coins"]

    home = web.get("/")
    assert home.status_code == 200
    for href in links:
        assert f'href="{href}"' in home.text
        assert web.get(href).status_code == 200

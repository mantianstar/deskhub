"""采集中心「批量方式(页面)」的服务层：URL 映射、>50 风控、去重入库、单飞。

运行方式：`.venv/bin/python -m pytest tests/test_coins_explore_page.py -q`
不发真实网络请求：取页与库写入前都按需桩掉。
"""

from __future__ import annotations

import asyncio

import pytest

from app import config, db, repository
from app.coins import explore_page, numista_web


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    config.load()
    return tmp_path / "test.db"


def _catalogue_html(total: int, rows: list[tuple[int, str]]) -> str:
    body = "".join(
        f"""
  <div class="resultat_recherche">
    <div class="photo_avers"><a href="/{series_id}"><img src="https://en.numista.com/photos/a{series_id}.jpg"></a></div>
    <div class="photo_revers"><a href="/{series_id}"><img src="https://en.numista.com/photos/b{series_id}.jpg"></a></div>
    <div class="description_piece">
      <strong><a href="/{series_id}">
        {title}<br>
        1948-1950 </a></strong>
      <br>
      <em>Coins › Standard circulation coins</em><br>
      Bronze • 9.45&nbsp;g • ⌀&nbsp;31.0&nbsp;mm<br>
      KM# 1, N# {series_id} </div>
  </div>"""
        for series_id, title in rows
    )
    return (
        f'<div class="catalogue_navigation">{total} results found.</div>'
        '<div class="catalogue_search_results">'
        '<header id="e_afrique_du_sud"><h2>South Africa • Pound (1825-1961)</h2></header>'
        f"{body}</div>"
    )


def _run_once(item_html: str, batch_id: str, monkeypatch, issuer: str = "") -> None:
    async def fake_fetch(cfg, *, url=None):
        return item_html

    monkeypatch.setattr(numista_web, "fetch_catalogue_page", fake_fetch)
    explore_page._running = None
    explore_page._state.update(
        {"job_id": "job", "saved": 0, "skipped": 0, "parsed": 0, "percent": 0}
    )
    asyncio.run(
        explore_page._run(
            config.get(),
            job_id="job",
            batch_id=batch_id,
            url="https://example.test/list",
            issuer=issuer,
        )
    )


def test_download_images_without_urls_returns_empty(temp_db, monkeypatch):
    """Numista 无图片地址时不抛错，返回空字典（采集队列据此把图片节点记成功）。"""
    from types import SimpleNamespace

    monkeypatch.setattr(
        repository,
        "get_coin_series",
        lambda _id: SimpleNamespace(obverse_picture=None, reverse_picture=None),
    )
    assert asyncio.run(
        numista_web.download_series_images(config.get(), numista_id=990001)
    ) == {}


def test_page_url_maps_chinese_filters():
    filters = explore_page.normalize_filters(
        issuer="英国", st="1", year="1900-2026", material="5", size="30"
    )

    assert filters["issuer"] == "royaume-uni"
    url = explore_page.catalogue_page_url(**filters)
    assert "e=royaume-uni" in url
    assert "st=1" in url and "a=1900-2026" in url
    assert "m=5" in url and "t=30" in url


def test_page_url_accepts_english_and_slug():
    assert explore_page.resolve_issuer("United Kingdom") == "royaume-uni"
    assert explore_page.resolve_issuer("royaume-uni") == "royaume-uni"
    with pytest.raises(ValueError):
        explore_page.resolve_issuer("火星")


def test_run_page_explore_over_50_stops_before_write(temp_db, monkeypatch):
    _run_once(_catalogue_html(51, [(1, "1 Penny - George VI")]), "b-over", monkeypatch)

    assert repository.list_coin_page_explore_items("b-over") == []
    state = explore_page.status()
    assert state["status"] == "failed"
    assert state["error_code"] == "over_limit"
    assert state["error_message"] == explore_page.OVER_LIMIT_MESSAGE


def test_run_page_explore_upserts_and_skips_existing(temp_db, monkeypatch):
    html = _catalogue_html(
        2,
        [(6699, "1 Penny - George VI"), (7199, "5 Centimos - Isabella II")],
    )
    _run_once(html, "b-ok", monkeypatch)

    rows = repository.list_coin_page_explore_items("b-ok")
    assert [row.numista_id for row in rows] == [6699, 7199]
    assert rows[0].issuer_cn == "南非"
    assert rows[0].composition_cn == "青铜"
    # 无大模型 Key：名称保留英文原文，不做半截词表替换（避免中英混排）
    assert rows[0].title_cn == rows[0].title
    assert explore_page.status()["saved"] == 2

    # 再抓一次：已存在的直接跳过，不新增行
    _run_once(html, "b-ok", monkeypatch)
    assert len(repository.list_coin_page_explore_items("b-ok")) == 2
    state = explore_page.status()
    assert (state["saved"], state["skipped"]) == (0, 2)


def test_run_page_explore_reskipped_ids_visible_in_new_batch(temp_db, monkeypatch):
    html = _catalogue_html(
        2,
        [(6699, "1 Penny - George VI"), (7199, "5 Centimos - Isabella II")],
    )
    _run_once(html, "b-first", monkeypatch)
    _run_once(html, "b-second", monkeypatch)

    # 第二次全冲突：行仍挂老批次，但新批次页要按成员表把这两条查出来
    assert explore_page.status()["skipped"] == 2
    rows = repository.list_coin_page_explore_items("b-second")
    assert [row.numista_id for row in rows] == [6699, 7199]
    assert rows[0].batch_id == "b-first"


def test_run_page_explore_backfills_issuer_from_filter(temp_db, monkeypatch):
    # 按国家筛选时分组头只剩货币名（`Pataca (1894-date)`），整页国家要回填筛选值
    html = _catalogue_html(1, [(9001, "5 Patacas - João IV")]).replace(
        "South Africa • Pound (1825-1961)", "Pataca (1894-date)"
    )

    _run_once(html, "b-macao", monkeypatch, issuer="macao")

    row = repository.list_coin_page_explore_items("b-macao")[0]
    assert row.issuer == "Macau"
    assert row.issuer_cn == "澳门"


def test_run_page_explore_single_flight(temp_db, monkeypatch):
    async def scenario():
        async def hang(cfg, *, url=None):
            await asyncio.sleep(3600)

        monkeypatch.setattr(numista_web, "fetch_catalogue_page", hang)
        explore_page._running = None
        first = explore_page.start(
            config.get(), issuer="royaume-uni", st="1", year="", material="", size=""
        )
        assert first is not None
        assert (
            explore_page.start(
                config.get(), issuer="royaume-uni", st="1", year="", material="", size=""
            )
            is None
        )
        task = explore_page._running
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert explore_page.is_running() is False

    asyncio.run(scenario())


def test_run_page_explore_translates_short_texts_in_one_batch(temp_db, monkeypatch):
    calls: list[list[str]] = []

    async def fake_short_texts(client, texts):
        calls.append(list(texts))
        return {text: f"中文:{text}" for text in texts}

    monkeypatch.setattr(explore_page.llm, "get_client", lambda cfg: object())
    monkeypatch.setattr(explore_page.translate_llm, "short_texts_zh", fake_short_texts)
    explore_page._TEXT_CACHE.clear()
    html = _catalogue_html(1, [(4242, "10 Zzz Token (Special Issue)")])

    _run_once(html, "b-llm", monkeypatch)

    assert len(calls) == 1
    assert "10 Zzz Token (Special Issue)" in calls[0]
    row = repository.list_coin_page_explore_items("b-llm")[0]
    assert row.title_cn == "中文:10 Zzz Token (Special Issue)"
    assert row.denomination == "10 Zzz Token"
    assert row.ruler is None
    # 词表已能译出的国家 / 材质不进请求，仍以词表为准
    assert row.issuer_cn == "南非"
    assert row.composition_cn == "青铜"
    explore_page._TEXT_CACHE.clear()

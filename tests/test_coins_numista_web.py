"""Numista 类型页（估值 + Frequency）测试（spec §14.13 / plan §18.13.2）。

- **解析**：`app/coins/numista_web.py` 的纯函数，喂**真实页面切片**
  （`tests/fixtures/numista_type_670.html`，2026-10-14 用 CDP 挂 Chrome 抓的系列 670 表格，
  原样截取、未手工改写）。
- **落库**：桩掉 `fetch_type_page`（不发真实网络请求）跑 `collect_issue_values`，验幂等。
- **报错路径**：浏览器没挂上时给出可操作的提示（这是最容易踩的一步）。

运行方式：`.venv/bin/python -m pytest tests/test_coins_numista_web.py -q`
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config as config_module
from app import db, repository
from app.coins import numista_web

FIXTURE = Path(__file__).parent / "fixtures" / "numista_type_670.html"

# 真实页面里的 15 行（单版编号与 `/types/670/issues` 的 id 一一对应，顺序也一致）
PAGE_ISSUE_IDS = [
    116868, 45228, 116867, 5843, 116869, 3577, 681291, 6871, 116870, 58017,
    21548, 9598, 4229, 5844, 116871,
]


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


@pytest.fixture()
def page_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 解析（纯函数）


def test_parse_type_page_reads_currency_and_every_row(page_html):
    info = numista_web.parse_type_page(page_html)

    assert info.currency == "CNY"  # 币种是页面里读出来的（按访客币种渲染）
    assert [row.numista_issue_id for row in info.rows] == PAGE_ISSUE_IDS


def test_parse_type_page_maps_values_and_frequency(page_html):
    info = numista_web.parse_type_page(page_html)
    by_id = {row.numista_issue_id: row for row in info.rows}

    # 1898 普通版：7 个品相都有值
    row = by_id[21548]
    assert row.year == 1898
    assert (row.value_g, row.value_vg, row.value_f) == (2.3, 5.9, 8.8)
    assert (row.value_vf, row.value_xf, row.value_au, row.value_unc) == (12.0, 35.0, 130.0, 1200.0)
    assert row.frequency == 27.0

    # 1895 第一行是精制版：没有估值（留空），只有 Frequency
    proof = by_id[116868]
    assert proof.year == 1895
    assert proof.value_g is None and proof.value_unc is None
    assert proof.frequency == 0.16

    # 缺某个品相时只空那一格（1895 第二行没有 UNC；1897 high tide 只有 VF）
    assert by_id[45228].value_unc is None and by_id[45228].value_au == 41.0
    assert by_id[58017].value_vf == 130.0 and by_id[58017].value_g is None


def test_parse_type_page_skips_undetermined_placeholder_row(page_html):
    """页面的「Undetermined」占位行（编号 0、隐藏模板）不该被当成一行数据。"""
    info = numista_web.parse_type_page(page_html)

    assert len(info.rows) == 15
    assert all(row.numista_issue_id != 0 for row in info.rows)
    assert all(row.year is not None for row in info.rows)


def test_parse_type_page_without_table_returns_empty():
    info = numista_web.parse_type_page("<html><body>Checking connection…</body></html>")

    assert info.rows == [] and info.currency is None


# ---------------------------------------------------------------- 目录列表页（候选池）

# 真实页面切片（2026-10-18 抓的 South Africa / Spain 两条，结构原样保留）
CATALOGUE_HTML = """
<div class="catalogue_navigation">126 results found.</div>
<div class="catalogue_search_results">
  <header id="e_afrique_du_sud"><h2>South Africa • Pound (1825-1961)</h2>
    <p>1 Pound = 20 Shillings</p>
  </header>
  <div class="resultat_recherche">
    <div class="photo_avers"><a href="/6699"><img src="https://en.numista.com/photos/1130-180.jpg" alt="obverse"></a></div>
    <div class="photo_revers"><a href="/6699"><img src="https://en.numista.com/photos/1131-180.jpg" alt="reverse"></a></div>
    <div class="description_piece">
      <strong><a href="/6699">
        1 Penny - George VI (SOUTH AFRICA - SUID AFRIKA)<br>
        1948-1950 </a></strong>
      <br>
      <em>Coins › Standard circulation coins</em><br>
      Bronze • 9.45&nbsp;g • ⌀&nbsp;31.0&nbsp;mm<br>
      KM# 34.1, Hern# S108-110, N# 6699 </div>
  </div>
  <header id="e_espagne"><h2>Spain • Real (1497-1864)</h2></header>
  <div class="resultat_recherche">
    <div class="photo_avers"><a href="/7199"><img src="https://en.numista.com/photos/740-180.jpg" alt="obverse"></a></div>
    <div class="photo_revers"><a href="/7199"><img src="https://en.numista.com/photos/741-180.jpg" alt="reverse"></a></div>
    <div class="description_piece">
      <strong><a href="/7199">
        5 Centimos de Escudo - Isabella II<br>
        1865-1868 </a></strong>
      <br>
      <em>Coins › Standard circulation coins</em><br>
      Bronze • 12.5&nbsp;g • ⌀&nbsp;32&nbsp;mm<br>
      KM# 605, N# 7199 </div>
  </div>
</div>
"""


def test_parse_catalogue_index_reads_total_and_items():
    page = numista_web.parse_catalogue_index(CATALOGUE_HTML)

    assert page.total == 126
    assert [item.numista_id for item in page.items] == [6699, 7199]

    first = page.items[0]
    assert first.title == "1 Penny - George VI (SOUTH AFRICA - SUID AFRIKA)"
    assert (first.min_year, first.max_year) == (1948, 1950)
    assert first.issuer == "South Africa"  # 取自分组头，不是国旗图上的统治者
    assert first.composition == "Bronze"
    assert (first.weight_g, first.size_mm) == (9.45, 31.0)
    assert first.catalog_no == "KM# 34.1, Hern# S108-110, N# 6699"
    assert first.obverse_picture.endswith("1130-180.jpg")
    assert page.items[1].issuer == "Spain"


def test_parse_catalogue_index_empty_result():
    page = numista_web.parse_catalogue_index("<div>0 results found.</div>")

    assert page.total == 0 and page.items == []


def test_catalogue_url_uses_configured_filters():
    filters = config_module.ExploreCatalogueConfig(
        collectible_type=7,
        year="1900-1950",
        composition_id=3,
        diameter_mm="25-30",
        per_page=100,
    )
    url = numista_web.catalogue_url(filters, page=2)

    assert "st=7" in url and "a=1900-1950" in url
    assert "m=3" in url and "t=25-30" in url
    assert "p=2" in url and "q=100" in url


# ---------------------------------------------------------------- 落库（桩掉取页）


@pytest.mark.asyncio
async def test_collect_issue_values_stores_rows_idempotently(temp_db, page_html, monkeypatch):
    async def fake_fetch(cdp_url, type_id, *, timeout_seconds=30):
        assert type_id == 670
        return page_html

    monkeypatch.setattr(numista_web, "fetch_type_page", fake_fetch)

    cfg = config_module.load()
    first = await numista_web.collect_issue_values(cfg, series_id=670)
    await numista_web.collect_issue_values(cfg, series_id=670)  # 重抓不增行

    assert len(first.rows) == 15
    assert repository.count_coin_issue_values() == 15

    stored = repository.get_coin_issue_values(21548)
    assert stored is not None
    assert (stored.series_numista_id, stored.currency) == (670, "CNY")
    assert (stored.value_g, stored.value_unc, stored.frequency) == (2.3, 1200.0, 27.0)

    rows = repository.list_coin_issue_values(670)
    assert [row.numista_id for row in rows] == sorted(PAGE_ISSUE_IDS)
    assert all(row.fetched_at for row in rows)


# ---------------------------------------------------------------- 报错路径


@pytest.mark.asyncio
async def test_fetch_type_page_guides_user_when_browser_missing(temp_db):
    """没挂上 CDP 浏览器时要给出「怎么开 Chrome」的提示，而不是丢一句报错。"""
    with pytest.raises(numista_web.NumistaPageError) as excinfo:
        await numista_web.fetch_type_page("http://127.0.0.1:1", 670, timeout_seconds=1)

    message = str(excinfo.value)
    assert "remote-debugging-port" in message or "playwright" in message

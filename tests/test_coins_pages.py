"""铜币页面测试：专场列表 / 拍品页 / 画像编辑（M7、M9，plan §18.8/§18.11）
+ 参考库信息页 / 系列详情 / 采集管理（M14，plan §18.14）。

运行方式：`.venv/bin/python -m pytest tests/test_coins_pages.py -q`
走真实 lifespan（建库 + 同步源清单），但调度与启动补拉走空壳，不发真实网络请求。
M14 的采集按钮在用例里一律把 `numista.collect_*` 换成桩，不发真实网络请求。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import config, repository
from app.coins import numista as coins_numista
from app.coins import numista_web as coins_numista_web
from app.coins.numista import NumistaError
from app.coins.numista_web import NumistaPageError
from app.main import app as fastapi_app
from app.models import (
    CoinIssue,
    CoinIssueValues,
    CoinLot,
    CoinPriceRef,
    CoinSeries,
    CoinSession,
    CoinShop,
)


@pytest.fixture()
def web(tmp_path, monkeypatch, no_scheduler):
    monkeypatch.setenv("DESKHUB_DB_PATH", str(tmp_path / "coins.db"))
    config.reset()
    with TestClient(fastapi_app) as client:
        yield client
    config.reset()


def _seed_session() -> int:
    """造一个「有推荐拍品」的专场，返回 session_id。"""
    now = repository.utcnow()
    shop_id = repository.upsert_coin_shop(
        CoinShop(
            id=None,
            mxiqi_uid="49528",
            name="猩哥外币小铺",
            avatar_url="https://img.example/avatar.png",
            badges=[{"kind": "certified", "label": "认证"}, {"kind": "years", "label": "2年店铺"}],
            fetched_at=now,
        )
    )
    session_id = repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id="328062",
            shop_id=shop_id,
            title="猩哥外币小铺➡【东印度硬币专场】",
            status="running",
            auction_at="2026-10-08T07:25:00Z",
            auction_at_raw="10月8日 星期四 15:25",
            deposit=10.0,
            lot_total=50,
            view_count=79,
            cover_urls=[f"https://img.example/c{i}.png" for i in range(1, 6)],
            fetched_at=now,
        )
    )
    recommended = repository.upsert_coin_lot(
        CoinLot(
            id=None,
            session_id=session_id,
            mxiqi_id="lot-1",
            lot_no="Lot.1",
            title="东印度公司1808年硬币",
            start_price=15.0,
            current_price=0.0,
            image_urls=[f"https://img.example/l{i}.png" for i in range(1, 5)],
            tags={"发行年份": "1808", "国家地区": "印度"},
            match_score=2.4,
            is_recommended=True,
            fetched_at=now,
        )
    )
    # 一条未命中的拍品：不应出现在拍品页
    repository.upsert_coin_lot(
        CoinLot(
            id=None,
            session_id=session_id,
            mxiqi_id="lot-2",
            lot_no="Lot.9",
            title="某无关的纪念章",
            start_price=1.0,
            current_price=0.0,
            image_urls=[],
            tags={},
            match_score=0.0,
            is_recommended=False,
            fetched_at=now,
        )
    )
    repository.upsert_coin_price_ref(
        CoinPriceRef(
            lot_id=recommended,
            keyword="东印度 1808 硬币",
            sample_count=5,
            p25=12.0,
            median=18.0,
            p75=30.0,
            raw='[{"title":"东印度1808年","price":18}]',
            queried_at=now,
        )
    )
    return session_id


# ---------------------------------------------------------------- 第一页


def test_sessions_page_shows_only_sessions_with_hits(web):
    session_id = _seed_session()
    # 再插一个没有任何推荐拍品的专场 → 不该出现在列表里
    now = repository.utcnow()
    repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id="emptysession",
            shop_id=None,
            title="一个没有命中拍品的专场",
            status="running",
            auction_at=None,
            auction_at_raw=None,
            deposit=None,
            lot_total=3,
            view_count=1,
            cover_urls=[],
            fetched_at=now,
        )
    )

    page = web.get("/coins")
    assert page.status_code == 200
    assert "东印度硬币专场" in page.text
    assert "一个没有命中拍品的专场" not in page.text  # 「没有就不展示」
    assert "10月8日 星期四 15:25" in page.text  # 站点原文时间
    assert "共 50 件拍品" in page.text
    assert "79 人次围观" in page.text
    assert "命中 1 件" in page.text
    assert f'href="/coins/session/{session_id}"' in page.text
    assert page.text.count("coin-thumb") >= 4  # 4 张商品图


def test_sessions_page_empty_state(web):
    page = web.get("/coins")
    assert page.status_code == 200
    assert "没有与你画像匹配的专场" in page.text


# ---------------------------------------------------------------- 第二页


def test_session_page_renders_shop_header_and_recommended_lots(web):
    session_id = _seed_session()

    page = web.get(f"/coins/session/{session_id}")
    assert page.status_code == 200
    # 店铺头字段
    assert "猩哥外币小铺" in page.text
    assert "认证" in page.text and "2年店铺" in page.text
    assert "押金 ¥10" in page.text
    assert "共 50 件" in page.text
    assert "推荐 1 件" in page.text
    assert "79 人次围观" in page.text
    # 拍品字段
    assert "东印度公司1808年硬币" in page.text
    assert "15" in page.text  # 起价
    # 推荐价：中位数 + 区间 + 样本数 + 关键词
    assert "推荐价 ¥18" in page.text
    assert "12–30 元" in page.text
    assert "参考 5 条成交" in page.text
    assert "东印度 1808 硬币" in page.text
    # 未命中的拍品不出现
    assert "某无关的纪念章" not in page.text
    # 最多 3 张图
    assert page.text.count("coin-lot-img") >= 3


def test_session_page_404_for_unknown(web):
    page = web.get("/coins/session/999999")
    assert page.status_code == 404
    assert "这个专场不存在" in page.text


def test_session_page_shows_sample_insufficient_without_point_estimate(web):
    session_id = _seed_session()
    # 把取证改成样本不足（2 条）：不应出现单一推荐价，但区间还在
    lot_id = repository.list_coin_lots_for_matching()[0].id
    repository.upsert_coin_price_ref(
        CoinPriceRef(
            lot_id=lot_id,
            keyword="东印度 1808",
            sample_count=2,
            p25=10.0,
            median=None,
            p75=20.0,
            raw="[]",
            queried_at=repository.utcnow(),
        )
    )

    page = web.get(f"/coins/session/{session_id}")
    assert "样本不足（仅 2 条）" in page.text
    assert "推荐价 ¥" not in page.text


def test_session_page_shows_no_sample_marker(web):
    session_id = _seed_session()
    lot_id = repository.list_coin_lots_for_matching()[0].id
    repository.upsert_coin_price_ref(
        CoinPriceRef(
            lot_id=lot_id,
            keyword="东印度 1808",
            sample_count=0,
            p25=None,
            median=None,
            p75=None,
            raw="[]",
            queried_at=repository.utcnow(),
        )
    )

    page = web.get(f"/coins/session/{session_id}")
    assert "无成交样本，不报价" in page.text


# ---------------------------------------------------------------- 画像页与编辑


def test_profile_page_lists_tags(web):
    repository.upsert_coin_tag("country", "英国", 1.0, 12)
    repository.upsert_coin_tag("denomination", "一分", 0.6, 7)

    page = web.get("/coins/profile")
    assert page.status_code == 200
    assert "英国" in page.text
    assert "一分" in page.text
    assert "国家/地区" in page.text


def test_profile_toggle_weight_add_delete(web):
    repository.upsert_coin_tag("country", "英国", 1.0, 12)
    tag = repository.list_coin_profile_tags()[0]

    assert web.post(f"/coins/profile/{tag.id}/toggle", follow_redirects=False).status_code == 302
    assert repository.get_coin_tag(tag.id).enabled is False

    assert (
        web.post(
            f"/coins/profile/{tag.id}/weight", data={"weight": "0.3"}, follow_redirects=False
        ).status_code
        == 302
    )
    assert repository.get_coin_tag(tag.id).weight == 0.3

    add = web.post(
        "/coins/profile/add",
        data={"dimension": "condition", "value": "原光"},
        follow_redirects=False,
    )
    assert add.status_code == 302
    assert any(t.value == "原光" for t in repository.list_coin_profile_tags())

    assert web.post(f"/coins/profile/{tag.id}/delete", follow_redirects=False).status_code == 302
    assert repository.get_coin_tag(tag.id) is None


def test_profile_add_rejects_bad_dimension_and_unknown_tag(web):
    assert (
        web.post("/coins/profile/add", data={"dimension": "nope", "value": "x"}).status_code == 400
    )
    assert (
        web.post("/coins/profile/add", data={"dimension": "country", "value": "  "}).status_code
        == 400
    )
    assert web.post("/coins/profile/999999/toggle", follow_redirects=False).status_code == 404


# ---------------------------------------------------------------- 参考库：查询 / 详情 / 采集管理（M14）


def _seed_series() -> int:
    """造一个系列（中文表 + 英文表，与采集落库一致）+ 2 个年份单版 + 1 条估值。"""
    now = repository.utcnow()
    series = CoinSeries(
        id=None,
        numista_id=670,
        title="1 便士",
        url="https://en.numista.com/catalogue/pieces670.html",
        object_type="标准流通币",
        issuer="英国",
        min_year=1808,
        max_year=1860,
        ruler="George III",
        denomination="1 便士",
        size_mm=20.0,
        weight_g=3.3,
        thickness_mm=1.5,
        shape="圆形",
        composition="铜",
        edge="齿纹",
        obverse_desc="Laureate bust right",
        obverse_lettering="GEORGIUS III",
        obverse_picture="https://img.example/obv.png",
        reverse_desc="Britannia seated",
        reverse_lettering="BRITANNIA",
        reverse_picture="https://img.example/rev.png",
        catalog_no="KM#790,Sp#3961",
        comments=None,
        mints=["Royal Mint"],
        fetched_at=now,
    )
    series_id = repository.upsert_coin_series_cn(series)
    repository.upsert_coin_series(series)  # 英文表也写一份（采集落库就是这么写的）
    repository.upsert_coin_issue_cn(
        CoinIssue(
            id=None,
            numista_id=9001,
            series_id=series_id,
            year=1808,
            mint="Royal Mint",
            mint_mark=None,
            variety=None,
            comment=None,
            mintage=120000,
            size_mm=20.0,
            weight_g=3.3,
            thickness_mm=1.5,
            edge="齿纹",
            is_proof=False,
            is_essai=None,
            picture="https://img.example/i1.png",
            catalog_no="Sp#1",
            fetched_at=now,
        )
    )
    repository.upsert_coin_issue_cn(
        CoinIssue(
            id=None,
            numista_id=9002,
            series_id=series_id,
            year=1809,
            mint="Royal Mint",
            mint_mark=None,
            variety=None,
            comment=None,
            mintage=95000,
            size_mm=20.0,
            weight_g=3.3,
            thickness_mm=1.5,
            edge="齿纹",
            is_proof=False,
            is_essai=None,
            picture=None,
            catalog_no="Sp#2",
            fetched_at=now,
        )
    )
    repository.upsert_coin_issue_values(
        CoinIssueValues(
            id=None,
            numista_id=9001,
            series_numista_id=670,
            currency="CNY",
            value_g=5.0,
            value_vg=10.0,
            value_f=20.0,
            value_vf=50.0,
            value_xf=120.0,
            value_au=300.0,
            value_unc=800.0,
            frequency=0.16,
            fetched_at=now,
        )
    )
    return 670


def test_info_page_empty_state(web):
    page = web.get("/coins/info")
    assert page.status_code == 200
    assert "本地没有符合条件的系列" in page.text


def _make_series(numista_id: int, denomination: str, composition: str | None) -> CoinSeries:
    """造一条英国 1902–1910 的系列（1904 落在区间内），用于面值 / 材质筛选。"""
    return CoinSeries(
        id=None,
        numista_id=numista_id,
        title=denomination,
        url=None,
        object_type="标准流通币",
        issuer="英国",
        min_year=1902,
        max_year=1910,
        ruler="Edward VII",
        denomination=denomination,
        size_mm=None,
        weight_g=None,
        thickness_mm=None,
        shape="圆形",
        composition=composition,
        edge=None,
        obverse_desc=None,
        obverse_lettering=None,
        obverse_picture=None,
        reverse_desc=None,
        reverse_lettering=None,
        reverse_picture=None,
        catalog_no=None,
        comments=None,
        mints=[],
        fetched_at=repository.utcnow(),
    )


def _seed_uk_1904() -> None:
    """真实库英国 1904 命中的三条系列：半便士（青铜）/ 1便士（青铜）/ 1便士（银 .925）。"""
    for numista_id, denomination, composition in (
        (1867, "半 便士", "青铜"),
        (857, "1 便士", "青铜"),
        (13222, "1 便士", "银 (.925)"),
    ):
        repository.upsert_coin_series_cn(_make_series(numista_id, denomination, composition))


def test_info_page_lists_and_filters(web):
    _seed_series()
    assert "英国 · 1 便士" in web.get("/coins/info").text
    assert "已采 2 个年份" in web.get("/coins/info").text
    # 年份落在 [1808, 1860] 内 → 命中
    assert "英国 · 1 便士" in web.get("/coins/info", params={"country": "英国", "year": "1808"}).text
    # 区间外 → 空
    assert "本地没有符合条件的系列" in web.get(
        "/coins/info", params={"country": "英国", "year": "1900"}
    ).text
    # 年份非整数 → 提示且忽略该条件（仍返回全部）
    assert "不是整数" in web.get("/coins/info", params={"year": "abc"}).text


def test_series_page_merges_year_info_and_values(web):
    """年份信息 + 估值合并为一张「各年份信息」表，并带上厂标 / 目录号 / 备注。"""
    _seed_series()
    page = web.get("/coins/series/670")
    assert page.status_code == 200
    assert "系列信息" in page.text
    assert "各年份信息" in page.text
    assert "各年份估值" not in page.text  # 已合并，不再单独成表
    # 年份数 ≠ 版别数：1808 / 1809 两年，各一条单版
    assert "2 个年份 · 2 个版别" in page.text
    assert "KM#790,Sp#3961" in page.text
    assert "Royal Mint" in page.text
    # 合并后的列：厂标（年份后）/ 目录号（UNC 后）/ 备注（目录号后）
    for header in ("厂标", "发行量", "UNC", "目录号", "备注", "稀有度"):
        assert header in page.text
    assert "120000" in page.text  # 发行量
    assert "0.16%" in page.text  # Frequency
    assert "CNY" in page.text


def test_series_page_image_click_zoom_markup(web, tmp_path, monkeypatch):
    """详情页图片带 `coin-zoom` 类 + 灯箱节点，供前端点击放大。"""
    image_dir = tmp_path / "coin_images"
    image_dir.mkdir(parents=True)
    (image_dir / "670-obverse.png").write_bytes(b"PNGBYTES")
    monkeypatch.setattr(coins_numista_web, "IMAGE_DIR", image_dir)
    _seed_series()
    page = web.get("/coins/series/670")
    assert 'class="coin-zoom"' in page.text
    assert 'id="coin-lightbox"' in page.text


def test_series_page_404_for_unknown(web):
    assert web.get("/coins/series/999999").status_code == 404


def test_series_page_no_issues_empty_state(web):
    now = repository.utcnow()
    repository.upsert_coin_series_cn(
        CoinSeries(
            id=None,
            numista_id=888,
            title="半便士",
            url=None,
            object_type=None,
            issuer="英国",
            min_year=1860,
            max_year=1894,
            ruler="Victoria",
            denomination="半便士",
            size_mm=None,
            weight_g=None,
            thickness_mm=None,
            shape=None,
            composition=None,
            edge=None,
            obverse_desc=None,
            obverse_lettering=None,
            obverse_picture=None,
            reverse_desc=None,
            reverse_lettering=None,
            reverse_picture=None,
            catalog_no=None,
            comments=None,
            mints=[],
            fetched_at=now,
        )
    )
    page = web.get("/coins/series/888")
    assert "单版信息未采集" in page.text


def test_series_page_issues_without_values_hint(web):
    """有单版、没估值 → 表照渲（估值列留空），另给「估值未采集」的补齐提示。"""
    _seed_series()
    # 把 670 的估值删掉，模拟「只取了年份信息、还没取估值」
    repository.delete_coin_series(670)
    series = _make_series(670, "1 便士", "青铜")
    series_id = repository.upsert_coin_series_cn(series)
    repository.upsert_coin_issue_cn(
        CoinIssue(
            id=None,
            numista_id=9001,
            series_id=series_id,
            year=1808,
            mint=None,
            mint_mark=None,
            variety=None,
            comment=None,
            mintage=120000,
            size_mm=None,
            weight_g=None,
            thickness_mm=None,
            edge=None,
            is_proof=None,
            is_essai=None,
            picture=None,
            catalog_no="Sp#1",
            fetched_at=repository.utcnow(),
        )
    )
    page = web.get("/coins/series/670")
    assert "各年份信息" in page.text
    assert "估值未采集" in page.text


def test_year_stats_counts_distinct_years_not_issues(web):
    """年份统计修正：一个年份多个版别时，年份数按不同年份算，不按单版条数。"""
    series = _make_series(670, "1 便士", "青铜")
    series_id = repository.upsert_coin_series_cn(series)
    now = repository.utcnow()
    # 3 个版别，但只落在 2 个年份上
    for numista_id, year in ((1, 1895), (2, 1895), (3, 1896)):
        repository.upsert_coin_issue_cn(
            CoinIssue(
                id=None,
                numista_id=numista_id,
                series_id=series_id,
                year=year,
                mint=None,
                mint_mark=None,
                variety=None,
                comment=None,
                mintage=None,
                size_mm=None,
                weight_g=None,
                thickness_mm=None,
                edge=None,
                is_proof=None,
                is_essai=None,
                picture=None,
                catalog_no=None,
                fetched_at=now,
            )
        )
    stats = repository.coin_series_issue_stats(cn=True)[670]
    assert stats.year_count == 2
    assert stats.issue_count == 3
    assert "2 个年份 · 3 个版别" in web.get("/coins/info").text
    assert "年份 2 个 · 3 版" in web.get("/coins/manage").text


def test_manage_page_shows_collection_status(web, tmp_path, monkeypatch):
    """管理页要能看出「有没有年份信息 / 估值 / 图片」，不用点开详情。"""
    image_dir = tmp_path / "coin_images"
    image_dir.mkdir(parents=True)
    (image_dir / "670-obverse.png").write_bytes(b"PNGBYTES")  # 只有正面图
    monkeypatch.setattr(coins_numista_web, "IMAGE_DIR", image_dir)
    _seed_series()  # 670：2 个版别 + 1 条估值

    page = web.get("/coins/manage", params={"country": "英国"}).text
    assert "已采集状态" in page
    assert "年份 2 个 · 2 版" in page  # 年份信息已采
    assert "估值 1 条" in page  # 估值已采（1 条）
    assert "图片 1/2" in page  # 图片只缓存了正面
    assert "年份 未采" not in page

    # 未采集的系列 → 三个都显示「未采」
    repository.upsert_coin_series_cn(_make_series(999, "5 便士", "青铜"))
    page = web.get("/coins/manage").text
    assert "年份 未采" in page
    assert "估值 未采" in page
    assert "图片 未采" in page


def test_coin_series_value_counts_aggregates_by_series(web):
    """`coin_series_value_counts` 按系列号聚合估值条数（供管理页状态列）。"""
    _seed_series()  # 670 有 1 条估值
    counts = repository.coin_series_value_counts()
    assert counts.get(670) == 1
    assert 999 not in counts


def test_manage_page_renders_form_and_results(web):
    _seed_series()
    page = web.get("/coins/manage", params={"country": "英国", "year": "1808"})
    assert page.status_code == 200
    assert "① 获取系列" in page.text
    assert "② 获取年份信息" in page.text
    assert "③ 获取年份估值" in page.text
    assert "④ 下载图片" in page.text
    assert "英国 · 1 便士" in page.text
    assert "/coins/series/670" in page.text
    assert "年份 2 个 · 2 版" in page.text


def test_info_page_filters_by_denomination_and_composition(web):
    """信息页：面值 / 材质筛选生效；输入「1便士」（无空格）能对上库里的「1 便士」。"""
    _seed_uk_1904()
    # 面值：只留「1 便士」，不带出「半 便士」
    page = web.get(
        "/coins/info", params={"country": "英国", "year": "1904", "denomination": "1便士"}
    )
    assert "/coins/series/857" in page.text
    assert "/coins/series/13222" in page.text
    assert "/coins/series/1867" not in page.text
    # 材质：青铜 → 把「银 (.925)」筛掉
    page = web.get(
        "/coins/info", params={"country": "英国", "year": "1904", "composition": "青铜"}
    )
    assert "/coins/series/857" in page.text
    assert "/coins/series/1867" in page.text
    assert "/coins/series/13222" not in page.text


def test_manage_page_filters_results_by_denomination(web):
    """用户报的问题：查「英国 1904 1便士」，结果区不该把「半便士」也列出来。"""
    _seed_uk_1904()
    page = web.get(
        "/coins/manage", params={"country": "英国", "year": "1904", "denomination": "1便士"}
    )
    assert page.status_code == 200
    assert "/coins/series/857" in page.text
    assert "/coins/series/13222" in page.text
    assert "/coins/series/1867" not in page.text


def test_manage_page_filters_results_by_composition(web):
    _seed_uk_1904()
    page = web.get(
        "/coins/manage", params={"country": "英国", "year": "1904", "composition": "青铜"}
    )
    assert "/coins/series/13222" not in page.text  # 银 (.925) 被筛掉


def test_search_coin_series_number_boundary(web):
    """数字边界：`1便士` 不得咬进 `11便士`（同 spec §14.11）。"""
    repository.upsert_coin_series_cn(_make_series(1, "1 便士", "青铜"))
    repository.upsert_coin_series_cn(_make_series(2, "11 便士", "青铜"))
    hits = repository.search_coin_series(denomination="1便士", cn=True)
    assert {s.numista_id for s in hits} == {1}


def test_manage_page_passes_composition_through_redirect(web, monkeypatch):
    """按钮 1 成功后跳转 URL 要带上材质，回到结果区仍按材质筛选。"""

    async def fake_collect_series(_config, *, country, year, denomination, **_kw):
        return []

    monkeypatch.setattr(coins_numista, "collect_series", fake_collect_series)
    resp = web.post(
        "/coins/manage/fetch-series",
        data={"country": "英国", "year": "1904", "denomination": "1便士", "composition": "青铜"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "composition=%E9%9D%92%E9%93%9C" in resp.headers["location"]


def test_manage_fetch_series_calls_collect_and_redirects(web, monkeypatch):
    calls: list[tuple] = []

    async def fake_collect_series(_config, *, country, year, denomination, **_kw):
        calls.append((country, year, denomination))
        return []

    monkeypatch.setattr(coins_numista, "collect_series", fake_collect_series)
    resp = web.post(
        "/coins/manage/fetch-series",
        data={"country": "英国", "year": "1808", "denomination": "1便士"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/coins/manage?")
    assert calls == [("英国", 1808, "1便士")]


def test_manage_fetch_issues_calls_collect_and_redirects(web, monkeypatch):
    calls: list[int] = []

    async def fake_collect_issues(_config, *, series_id, **_kw):
        calls.append(series_id)
        return (None, [])

    monkeypatch.setattr(coins_numista, "collect_issues", fake_collect_issues)
    resp = web.post(
        "/coins/manage/fetch-issues",
        data={"series_id": "670", "country": "英国", "year": "1808", "denomination": "1便士"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert calls == [670]


def test_manage_fetch_values_calls_collect_and_redirects(web, monkeypatch):
    """按钮 3：复用 `numista_web.collect_issue_values`，成功后 302 回管理页。"""
    calls: list[int] = []

    async def fake_collect_values(_config, *, series_id, **_kw):
        calls.append(series_id)
        return None

    monkeypatch.setattr(coins_numista_web, "collect_issue_values", fake_collect_values)
    resp = web.post(
        "/coins/manage/fetch-values",
        data={"series_id": "670", "country": "英国", "year": "1808", "denomination": "1便士"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/coins/manage?")
    assert calls == [670]


def test_manage_download_images_calls_download_and_redirects(web, monkeypatch):
    """按钮 4：复用 `numista_web.download_series_images`，成功后 302 回管理页。"""
    calls: list[int] = []

    async def fake_download(_config, *, numista_id, **_kw):
        calls.append(numista_id)
        return {}

    monkeypatch.setattr(coins_numista_web, "download_series_images", fake_download)
    resp = web.post(
        "/coins/manage/download-images",
        data={"series_id": "670", "country": "英国"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert calls == [670]


def test_manage_fetch_values_shows_page_error_in_place(web, monkeypatch):
    """挂不上 CDP 浏览器时，就地提示（200，不 500）。"""

    async def boom(*_args, **_kwargs):
        raise NumistaPageError("挂不上 CDP 浏览器 http://127.0.0.1:9222")

    monkeypatch.setattr(coins_numista_web, "collect_issue_values", boom)
    resp = web.post("/coins/manage/fetch-values", data={"series_id": "670", "country": "英国"})
    assert resp.status_code == 200
    assert "挂不上 CDP 浏览器" in resp.text


def test_manage_fetch_series_shows_numista_error_in_place(web, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise NumistaError("不认识的国家 '天狼星'；请在 ISSUER_CODES 里补一行")

    monkeypatch.setattr(coins_numista, "collect_series", boom)
    resp = web.post(
        "/coins/manage/fetch-series",
        data={"country": "天狼星", "year": "1808", "denomination": "1便士"},
    )
    assert resp.status_code == 200
    assert "不认识的国家" in resp.text


def test_manage_fetch_series_validates_input(web):
    # 缺面值 / 缺国家 / 缺年份 → 就地提示（200，不重定向）
    assert "面值不能为空" in web.post(
        "/coins/manage/fetch-series", data={"country": "英国", "year": "1808", "denomination": ""}
    ).text
    assert "请选择国家" in web.post(
        "/coins/manage/fetch-series", data={"country": "", "year": "1808", "denomination": "1便士"}
    ).text
    assert "年份不能为空" in web.post(
        "/coins/manage/fetch-series", data={"country": "英国", "year": "", "denomination": "1便士"}
    ).text


# ---------------------------------------------------------------- 图片本地缓存（M15）
# Numista 图片是 `cross-origin-resource-policy: same-origin` 的跨站资源，页面里嵌不了、
# 裸 HTTP 又吃 Cloudflare 403 → 只能由 `coins-images` 借 CDP 下载到本地再读。
# 用例把 `IMAGE_DIR` 指到临时目录，并桩掉 CDP 取字节，不发真实网络请求。


def test_series_image_route_404_and_fallback_when_not_cached(web, tmp_path, monkeypatch):
    _seed_series()
    monkeypatch.setattr(coins_numista_web, "IMAGE_DIR", tmp_path / "empty")
    assert web.get("/coins/series/670/image/obverse").status_code == 404
    # 非法 side 也 404（不瞎猜）
    assert web.get("/coins/series/670/image/sideways").status_code == 404
    page = web.get("/coins/series/670")
    assert "Numista 图片不允许跨站引用" in page.text
    assert "image/obverse" not in page.text


def test_series_image_route_serves_cached_file(web, tmp_path, monkeypatch):
    _seed_series()
    image_dir = tmp_path / "coin_images"
    image_dir.mkdir(parents=True)
    (image_dir / "670-obverse.png").write_bytes(b"PNGBYTES")
    monkeypatch.setattr(coins_numista_web, "IMAGE_DIR", image_dir)

    resp = web.get("/coins/series/670/image/obverse")
    assert resp.status_code == 200
    assert resp.content == b"PNGBYTES"
    # 只有正面图 → 页面渲染 <img>，背面走「原图链接」
    assert "/coins/series/670/image/obverse" in web.get("/coins/series/670").text


def test_info_page_renders_cached_images_only(web, tmp_path, monkeypatch):
    """信息页列表卡也只渲染本地缓存的图，不外链 Numista（否则必是破图）。"""
    _seed_series()
    # 未缓存：不出现外链，也不出现本地图路由，给占位
    monkeypatch.setattr(coins_numista_web, "IMAGE_DIR", tmp_path / "empty")
    page = web.get("/coins/info")
    assert "img.example/obv.png" not in page.text
    assert "/coins/series/670/image/obverse" not in page.text
    assert "未缓存" in page.text

    # 缓存正面图后：只有正面走本地路由，背面仍是占位
    image_dir = tmp_path / "coin_images"
    image_dir.mkdir(parents=True)
    (image_dir / "670-obverse.png").write_bytes(b"PNGBYTES")
    monkeypatch.setattr(coins_numista_web, "IMAGE_DIR", image_dir)
    page = web.get("/coins/info")
    assert "/coins/series/670/image/obverse" in page.text
    assert "/coins/series/670/image/reverse" not in page.text


def test_manage_page_renders_cached_images_only(web, tmp_path, monkeypatch):
    """管理页结果行也要显示正 / 反图缩略图（只渲染本地缓存，未缓存给占位，不外链）。"""
    _seed_series()
    monkeypatch.setattr(coins_numista_web, "IMAGE_DIR", tmp_path / "empty")
    page = web.get("/coins/manage", params={"country": "英国"}).text
    assert "/coins/series/670/image/obverse" not in page
    assert "img.example/obv.png" not in page
    assert "未缓存" in page

    # 缓存正面图后：只有正面走本地路由，背面仍是占位
    image_dir = tmp_path / "coin_images"
    image_dir.mkdir(parents=True)
    (image_dir / "670-obverse.png").write_bytes(b"PNGBYTES")
    monkeypatch.setattr(coins_numista_web, "IMAGE_DIR", image_dir)
    page = web.get("/coins/manage", params={"country": "英国"}).text
    assert "/coins/series/670/image/obverse" in page
    assert "/coins/series/670/image/reverse" not in page


def test_download_series_images_writes_files_and_drops_stale(web, tmp_path, monkeypatch):
    _seed_series()
    image_dir = tmp_path / "coin_images"
    monkeypatch.setattr(coins_numista_web, "IMAGE_DIR", image_dir)
    image_dir.mkdir(parents=True)
    (image_dir / "670-obverse.jpg").write_bytes(b"stale")  # 旧后缀，应被清掉

    async def fake_fetch(_cdp_url, _type_id, urls, *, timeout_seconds):
        return {side: b"IMG-" + side.encode() for side in urls}

    monkeypatch.setattr(coins_numista_web, "_fetch_bytes_via_cdp", fake_fetch)
    saved = asyncio.run(coins_numista_web.download_series_images(config.get(), numista_id=670))

    assert set(saved) == {"obverse", "reverse"}
    # 源 URL 是 .png → 新文件按原后缀存，旧 .jpg 被清理
    assert (image_dir / "670-obverse.png").read_bytes() == b"IMG-obverse"
    assert not (image_dir / "670-obverse.jpg").exists()
    assert (image_dir / "670-reverse.png").read_bytes() == b"IMG-reverse"


def test_download_series_images_rejects_unknown_series(web, tmp_path, monkeypatch):
    monkeypatch.setattr(coins_numista_web, "IMAGE_DIR", tmp_path / "coin_images")
    with pytest.raises(coins_numista_web.NumistaPageError):
        asyncio.run(coins_numista_web.download_series_images(config.get(), numista_id=999999))

"""铜币页面测试：专场列表 / 拍品页 / 画像编辑（M7、M9，plan §18.8/§18.11）
+ 参考库信息页 / 系列详情 / 采集管理（M14，plan §18.14）。

运行方式：`.venv/bin/python -m pytest tests/test_coins_pages.py -q`
走真实 lifespan（建库 + 同步源清单），但调度与启动补拉走空壳，不发真实网络请求。
M14 的采集按钮在用例里一律把 `numista.collect_*` 换成桩，不发真实网络请求。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import config, repository
from app.coins import grades as coins_grades
from app.coins import numista as coins_numista
from app.coins import numista_web as coins_numista_web
from app.coins.numista import NumistaError
from app.coins.numista_web import NumistaPageError
from app.main import app as fastapi_app
from app.models import (
    CoinExploreCandidate,
    CoinIssue,
    CoinIssueValues,
    CoinLot,
    CoinOrder,
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


def _today_auction_at() -> str:
    """今日 15:25（业务时区）的截拍时间，UTC ISO 存储格式。"""
    return repository.to_utc_str(
        datetime.now(config.get().app.tz).replace(
            hour=15, minute=25, second=0, microsecond=0
        )
    )


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
            auction_at=_today_auction_at(),
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
    assert "精准 1 件" in page.text
    assert f'href="/coins/session/{session_id}"' in page.text
    assert page.text.count("coin-thumb") >= 4  # 4 张商品图
    assert 'class="coin-thumb coin-zoom"' in page.text  # 点击可放大
    assert 'id="coin-lightbox"' in page.text


def test_sessions_page_filters_recommendation_and_type(web):
    """是否推荐默认只看推荐；全部展示无匹配专场；推荐类型可筛精准 / 相似。"""
    session_id = _seed_session()
    other_id = repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id="all-session",
            shop_id=None,
            title="一个无关专场",
            status="running",
            auction_at=_today_auction_at(),
            auction_at_raw=None,
            deposit=None,
            lot_total=1,
            view_count=0,
            cover_urls=[],
            fetched_at=repository.utcnow(),
        )
    )

    page = web.get("/coins")
    assert '<option value="yes" selected>是</option>' in page.text
    assert "一个无关专场" not in page.text

    page = web.get("/coins?rec=all")
    assert "一个无关专场" in page.text
    assert f'href="/coins/session/{other_id}"' in page.text

    page = web.get("/coins?rec=no")
    assert "一个无关专场" in page.text
    assert f'href="/coins/session/{session_id}"' not in page.text

    page = web.get("/coins?type=similar")
    assert f'href="/coins/session/{session_id}"' not in page.text

    page = web.get("/coins?type=precise")
    assert f'href="/coins/session/{session_id}"' in page.text


def test_sessions_page_paginates(web):
    """专场列表 12 个/页，避免一次性渲染过多卡片。"""
    for index in range(13):
        session_id = repository.upsert_coin_session(
            CoinSession(
                id=None,
                mxiqi_id=f"paged-session-{index}",
                shop_id=None,
                title=f"分页专场 {index:02d}",
                status="running",
                auction_at=_today_auction_at(),
                auction_at_raw=None,
                deposit=None,
                lot_total=1,
                view_count=0,
                cover_urls=[],
                fetched_at=repository.utcnow(),
            )
        )
        repository.upsert_coin_lot(
            CoinLot(
                id=None,
                session_id=session_id,
                mxiqi_id=f"paged-lot-{index}",
                lot_no="1",
                title=f"推荐拍品 {index:02d}",
                start_price=1.0,
                current_price=0.0,
                image_urls=[],
                tags={},
                match_score=1.0,
                is_recommended=True,
                fetched_at=repository.utcnow(),
            )
        )

    page = web.get("/coins")
    assert "共 13 个专场" in page.text
    assert "第 1 / 2 页" in page.text

    page = web.get("/coins?page=2")
    assert "第 2 / 2 页" in page.text


def test_session_page_paginates_other_lots(web):
    """拍品页只分页「其他拍品」，精准 / 相似推荐始终完整展示。"""
    session_id = _seed_session()
    now = repository.utcnow()
    for index in range(21):
        repository.upsert_coin_lot(
            CoinLot(
                id=None,
                session_id=session_id,
                mxiqi_id=f"other-lot-{index}",
                lot_no=str(index + 1),
                title=f"其他拍品 {index:02d}",
                start_price=1.0,
                current_price=0.0,
                image_urls=[],
                tags={},
                match_score=0.0,
                is_recommended=False,
                fetched_at=now,
            )
        )

    page = web.get(f"/coins/session/{session_id}")
    assert 'class="coin-section-count">22 件' in page.text
    assert "第 1 / 2 页" in page.text

    page = web.get(f"/coins/session/{session_id}?page=2")
    assert "第 2 / 2 页" in page.text
    assert "精准推荐 1 件" in page.text


def test_session_page_orders_lots_by_number(web):
    """Lot 号按数字排序（Lot.12 在 Lot.119 之前），不是字符串序。"""
    session_id = _seed_session()
    now = repository.utcnow()
    for lot_no in ("Lot.119", "Lot.12", "Lot.2"):
        repository.upsert_coin_lot(
            CoinLot(
                id=None,
                session_id=session_id,
                mxiqi_id=f"order-lot-{lot_no}",
                lot_no=lot_no,
                title=f"拍品 {lot_no}",
                start_price=1.0,
                current_price=0.0,
                image_urls=[],
                tags={},
                match_score=0.0,
                is_recommended=False,
                fetched_at=now,
            )
        )

    detail = repository.get_coin_session_detail(session_id)
    assert [lot.lot_no for lot in detail.lots] == [
        "Lot.1",
        "Lot.2",
        "Lot.9",
        "Lot.12",
        "Lot.119",
    ]


def test_sessions_page_filters_by_auction_date_defaults_today(web):
    """拍卖截止日期控件默认今日；选其他日期只出该日截拍的专场。"""
    session_id = _seed_session()
    today = datetime.now(config.get().app.tz).date().isoformat()

    page = web.get("/coins")
    assert page.status_code == 200
    assert f'name="auction_date" value="{today}"' in page.text
    assert f'href="/coins/session/{session_id}"' in page.text

    page = web.get("/coins", params={"auction_date": "2026-10-08"})
    assert f'href="/coins/session/{session_id}"' not in page.text
    assert "没有与你画像匹配的专场" in page.text


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
    assert "精准推荐 1 件" in page.text
    assert "79 人次围观" in page.text
    # 拍品字段
    assert "东印度公司1808年硬币" in page.text
    assert "15" in page.text  # 起价
    # 推荐价：中位数 + 区间 + 样本数 + 关键词
    assert "推荐价 ¥18" in page.text
    assert "12–30 元" in page.text
    assert "参考 5 条成交" in page.text
    assert "东印度 1808 硬币" in page.text
    # 拍品分三组：精准推荐 + 相似推荐 + 其他拍品（未命中也展示）
    assert "精准推荐" in page.text
    assert "相似推荐" in page.text
    assert "相似推荐 0 件" in page.text
    assert "其他拍品" in page.text
    assert "某无关的纪念章" in page.text
    # 最多 3 张图
    assert page.text.count("coin-lot-img") >= 3
    assert 'class="coin-lot-img coin-zoom"' in page.text  # 点击可放大


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


# ---------------------------------------------------------------- 旧版标签画像已移除


def test_legacy_profile_page_removed(web):
    """旧版标签画像页面已按 P0 重构移除；后端标签与兜底逻辑保留。"""
    assert web.get("/coins/profile").status_code == 404


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
            gregorian_year=1808,
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
            gregorian_year=1809,
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
    # ②③④ 动作条 + 回跳到本系列详情
    assert "② 获取年份信息" in page.text
    assert "③ 获取年份估值" in page.text
    assert "④ 下载图片" in page.text
    assert 'name="return_to" value="/coins/series/670"' in page.text
    # 合并后的列：厂标（年份后）/ 目录号（UNC 后）/ 备注（目录号后）
    for header in ("厂标", "发行量", "UNC", "目录号", "备注", "稀有度"):
        assert header in page.text
    assert "120000" in page.text  # 发行量
    assert "0.16%" in page.text  # Frequency
    assert "CNY" in page.text


def test_series_page_shows_gregorian_year_when_different(web):
    """年份与公历不一致时并显 `year(gregorian_year)`；一致时只显年份。"""
    series = _make_series(2317, "2 分", "青铜")
    series_id = repository.upsert_coin_series_cn(series)
    now = repository.utcnow()
    for numista_id, (year, gregorian_year) in enumerate(((6, 1873), (1895, 1895)), start=1):
        repository.upsert_coin_issue_cn(
            CoinIssue(
                id=None,
                numista_id=numista_id,
                series_id=series_id,
                year=year,
                gregorian_year=gregorian_year,
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

    page = web.get("/coins/series/2317")

    assert "6(1873)" in page.text
    assert "1895(1895)" not in page.text


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


def test_series_page_candidate_banner_and_adopt(web):
    """未采纳候选：显示横幅 + 采纳按钮（回跳本页），不显示补数据动作条。"""
    now = repository.utcnow()
    payload = {
        "numista_id": 999001,
        "title": "候选系列",
        "issuer": "法国",
        "denomination": "10 分",
        "min_year": 1900,
        "max_year": 1901,
    }
    repository.upsert_coin_explore_candidate(
        CoinExploreCandidate(
            id=None,
            numista_id=999001,
            seed_key="seed",
            seed_name="法国 · 10 分",
            size_diff=0.0,
            status="candidate",
            payload=payload,
            payload_cn=payload,
            issues=[],
            verified_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    page = web.get("/coins/series/999001")
    assert "来自候选池，尚未采纳" in page.text
    assert 'name="next" value="/coins/series/999001"' in page.text
    assert 'action="/coins/manage/fetch-issues"' not in page.text


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
            gregorian_year=1808,
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
                gregorian_year=year,
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


# ---------------------------------------------------------------- 历史购买品相录入（spec §14.16）


def _seed_orders() -> None:
    """四组可去重的完整组合 + 一条缺面值（应排除）；A1/A2 与 A5 用于验「同值不同写法」合并。"""
    for order_no, name, country, era, denomination in (
        ("A1", "英国1808年一分", "英国", "1808", "一分"),
        ("A2", "英国1808年一分（再买一枚）", "英国", "1808年", "1分"),
        ("A3", "印度1900年半安娜", "印度", "1900", "半安娜"),
        ("A5", "英属新西兰1940年半便士", "英属新西兰", "1940", "半便士"),
    ):
        repository.upsert_coin_order(
            CoinOrder(
                order_no=order_no,
                shop_name="小铺",
                item_name=name,
                price=10.0,
                country=country,
                era=era,
                denomination=denomination,
            )
        )
    repository.upsert_coin_order(
        CoinOrder(order_no="A4", shop_name="小铺", item_name="无名币", price=5.0, country="英国", era="1808")
    )


def _combos(*, country=None, era=None, denomination=None, grade=None):
    all_combos = coins_grades.build_combos(
        repository.list_coin_orders(), repository.list_coin_purchase_grades()
    )
    return coins_grades.filter_combos(
        all_combos, country=country, era=era, denomination=denomination, grade=grade
    )


def test_normalize_country_to_numista_style():
    assert coins_grades.normalize_country("英属新西兰") == "新西兰"
    assert coins_grades.normalize_country("英属澳大利亚") == "澳大利亚"
    assert coins_grades.normalize_country("荷属东印度") == "东印度"
    assert coins_grades.normalize_country("葡属澳门") == "澳门"
    # 没有前缀 / 会变空 → 原样
    assert coins_grades.normalize_country("英国") == "英国"
    assert coins_grades.normalize_country("  英国 ") == "英国"
    assert coins_grades.normalize_country("英属") == "英属"
    assert coins_grades.normalize_country(None) == ""


def test_normalize_year_to_numista_style():
    assert coins_grades.normalize_year("1910年") == "1910"
    assert coins_grades.normalize_year("1959") == "1959"
    assert coins_grades.normalize_year(" 1910 ") == "1910"
    # 年号纪年 → 取年份数字（与 Numista 的 year 对齐）
    assert coins_grades.normalize_year("康德四年") == "4"
    assert coins_grades.normalize_year("康德元年") == "1"
    assert coins_grades.normalize_year("明治十五年") == "15"
    assert coins_grades.normalize_year("明治15年") == "15"
    assert coins_grades.normalize_year("民国六十二年") == "62"
    # 只有年号、没有年份数字 → 原样保留
    assert coins_grades.normalize_year("光绪") == "光绪"


def test_normalize_denomination_to_numista_style():
    assert coins_grades.normalize_denomination("1便士") == "1 便士"
    assert coins_grades.normalize_denomination("一分") == "1 分"
    assert coins_grades.normalize_denomination("半便士") == "半 便士"
    assert coins_grades.normalize_denomination("二十文") == "20 文"
    assert coins_grades.normalize_denomination("五毫") == "5 毫"
    assert coins_grades.normalize_denomination("2.5比塞塔") == "2.5 比塞塔"
    # 大写中文数字与分数（真实订单里都有）
    assert coins_grades.normalize_denomination("壹圆") == "1 圆"
    assert coins_grades.normalize_denomination("拾分") == "10 分"
    assert coins_grades.normalize_denomination("1/4安娜") == "1/4 安娜"
    assert coins_grades.normalize_denomination("1/12先令") == "1/12 先令"
    assert coins_grades.normalize_denomination("¼安娜") == "四分之一 安娜"
    assert coins_grades.normalize_denomination("½便士") == "半 便士"
    # 已是统一写法 → 幂等
    assert coins_grades.normalize_denomination("1 便士") == "1 便士"
    assert coins_grades.normalize_denomination("半 便士") == "半 便士"
    # 认不出前导数字 → 原样保留
    assert coins_grades.normalize_denomination("当制钱十文") == "当制钱十文"
    assert coins_grades.normalize_denomination("大便士") == "大便士"


def test_grades_page_empty_state(web):
    page = web.get("/coins/grades")
    assert page.status_code == 200
    assert "没有可录入的历史记录" in page.text


def test_build_combos_dedupes_after_normalizing(web):
    _seed_orders()
    # 「1808」与「1808年」、「一分」与「1分」统一后合并成一行（order_count=2）；
    # 「英属新西兰」也统一成「新西兰」
    assert [
        (c.country, c.era, c.denomination, c.order_count, c.grade) for c in _combos()
    ] == [
        ("印度", "1900", "半 安娜", 1, None),
        ("新西兰", "1940", "半 便士", 1, None),
        ("英国", "1808", "1 分", 2, None),
    ]


def test_build_combos_includes_grades_without_orders(web):
    """手工新增的组合（历史订单里没有）也进列表，order_count=0。"""
    repository.upsert_coin_purchase_grade("英国", "1910", "1 便士", "好")
    assert [
        (c.country, c.era, c.denomination, c.order_count, c.grade) for c in _combos()
    ] == [("英国", "1910", "1 便士", 0, "好")]


def test_filter_combos(web):
    _seed_orders()
    assert [(c.era, c.denomination) for c in _combos(country="英国")] == [("1808", "1 分")]
    assert [(c.country, c.era) for c in _combos(denomination="半安娜")] == [("印度", "1900")]
    assert [(c.country, c.denomination) for c in _combos(era="1900")] == [("印度", "半 安娜")]
    # 用户按原写法查（带「年」）也能命中统一后的值
    assert [(c.country,) for c in _combos(era="1808年")] == [("英国",)]


def test_grades_page_lists_combos_with_dropdown(web):
    _seed_orders()
    page = web.get("/coins/grades")
    assert page.status_code == 200
    # 三个去重组合 → 结果表里三行（每行一个隐藏键 + 一个品相下拉 + 一个删除按钮）
    assert page.text.count('<input type="hidden" name="denomination"') == 3
    assert page.text.count('class="btn btn-ghost js-grade-del"') == 3
    # 行内下拉 ×3 + 「新增」表单下拉 ×1 + 查询表单的品相筛选 ×1
    assert page.text.count('<select name="grade"') == 5
    for label in ("未录入", "好", "普通", "差"):
        assert f">{label}</option>" in page.text
    # 查询表单的品相筛选
    assert '<option value="" selected>全部</option>' in page.text
    # 新增组合表单
    assert 'action="/coins/grades/add"' in page.text
    # 删除走页内二次确认
    assert "'/coins/grades/delete'" in page.text
    assert "确认删除" in page.text
    # 派生列「所属收藏系列」（不落库）
    assert "所属收藏系列" in page.text
    # 展示的是统一后的值
    assert "<td>1 分</td>" in page.text
    assert "<td>新西兰</td>" in page.text


def test_grades_page_derives_collection_series(web):
    """按国家 + 面值把组合归到收藏系列；匹配不上显示「—」。"""
    _seed_orders()
    repository.upsert_coin_collection_series(
        collection_key="uk-1c",
        name="英国 · 1 分",
        country="英国",
        ruler=None,
        denomination="1分",
    )
    page = web.get("/coins/grades")
    assert "英国 · 1 分" in page.text
    assert page.text.count("coin-grade-collection") == 3


def test_series_action_redirects_back_to_series(web, monkeypatch):
    """②③④ 带 return_to 时回系列详情，而不是回采集中心。"""
    _seed_series()

    async def fake_collect_issues(*_args, **_kwargs):
        return (None, [])

    monkeypatch.setattr(coins_numista, "collect_issues", fake_collect_issues)
    resp = web.post(
        "/coins/manage/fetch-issues",
        data={"series_id": "670", "return_to": "/coins/series/670"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/coins/series/670"


def test_grades_page_filters_by_grade(web):
    _seed_orders()
    repository.upsert_coin_purchase_grade("英国", "1808", "1 分", "好")
    repository.upsert_coin_purchase_grade("印度", "1900", "半 安娜", "差")

    # 结果里三行：英国=好、印度=差、新西兰=未录入
    assert [(c.country, c.grade) for c in _combos()] == [
        ("印度", "差"),
        ("新西兰", None),
        ("英国", "好"),
    ]
    assert [(c.country,) for c in _combos(grade="好")] == [("英国",)]

    # 页面：带品相条件只出对应行
    page = web.get("/coins/grades", params={"grade": "好"})
    assert page.text.count('<input type="hidden" name="denomination"') == 1
    assert "本次 1 个组合 · 已录 1" in page.text


def test_grades_save_upserts_and_clears(web):
    _seed_orders()
    resp = web.post(
        "/coins/grades/save",
        data={
            "country": ["英国", "印度"],
            "era": ["1808", "1900"],
            "denomination": ["一分", "半安娜"],
            "grade": ["好", "差"],
            "filter_country": "英国",
            "filter_year": "",
            "filter_denomination": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # 带回筛选条件重定向
    assert resp.headers["location"] == "/coins/grades?country=%E8%8B%B1%E5%9B%BD&year=&denomination="

    # 落库键是统一后的写法
    stored = {(g.country, g.era, g.denomination): g.grade for g in repository.list_coin_purchase_grades()}
    assert stored == {("英国", "1808", "1 分"): "好", ("印度", "1900", "半 安娜"): "差"}
    # 已录值回显
    assert 'value="好" selected' in web.get("/coins/grades").text

    # 选回「未录入」（空串）→ 清除该组合，另一行保留
    web.post(
        "/coins/grades/save",
        data={
            "country": ["英国", "印度"],
            "era": ["1808", "1900"],
            "denomination": ["一分", "半安娜"],
            "grade": ["好", ""],
        },
    )
    assert [g.grade for g in repository.list_coin_purchase_grades()] == ["好"]


def test_grades_save_rejects_bad_grade(web):
    _seed_orders()
    resp = web.post(
        "/coins/grades/save",
        data={
            "country": ["英国"],
            "era": ["1808"],
            "denomination": ["一分"],
            "grade": ["极品"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert repository.list_coin_purchase_grades() == []


def test_grades_add_creates_combo_and_normalizes(web):
    _seed_orders()
    resp = web.post(
        "/coins/grades/add",
        data={"country": "英国", "year": "1910年", "denomination": "1便士", "grade": "普通"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/coins/grades"
    # 年份 / 面值统一后入库，且该组合出现在列表里（历史订单里没有）
    assert [
        (g.country, g.era, g.denomination, g.grade) for g in repository.list_coin_purchase_grades()
    ] == [("英国", "1910", "1 便士", "普通")]
    assert [
        (c.country, c.era, c.denomination, c.order_count, c.grade) for c in _combos()
    ][-1] == ("英国", "1910", "1 便士", 0, "普通")


def test_grades_add_validates_input(web):
    _seed_orders()
    # 品相非法 / 国家为空 → 400，且不落库
    assert web.post(
        "/coins/grades/add",
        data={"country": "英国", "year": "1910", "denomination": "1便士", "grade": "极品"},
        follow_redirects=False,
    ).status_code == 400
    assert web.post(
        "/coins/grades/add",
        data={"country": "   ", "year": "1910", "denomination": "1便士", "grade": "好"},
        follow_redirects=False,
    ).status_code == 400
    assert repository.list_coin_purchase_grades() == []


def test_grades_delete_removes_orders_and_grade(web):
    """删整行：清掉该组合的**全部历史订单**（含不同写法）+ 品相记录，其余组合不动。"""
    _seed_orders()
    repository.upsert_coin_purchase_grade("英国", "1808", "1 分", "好")
    assert len(repository.list_coin_order_keys()) == 5  # A1/A2/A3/A5 + 缺面值的 A4

    resp = web.post(
        "/coins/grades/delete",
        # 传原始写法也能删（按统一后的组合比对）
        data={
            "country": "英国",
            "era": "1808年",
            "denomination": "一分",
            "filter_country": "英国",
            "filter_year": "",
            "filter_denomination": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/coins/grades?country=%E8%8B%B1%E5%9B%BD&year=&denomination="

    # A1（一分）与 A2（1分）都被删；缺面值的 A4、印度 A3、新西兰 A5 留下
    remaining = repository.list_coin_order_keys()
    assert {row[3] for row in remaining} == {None, "半安娜", "半便士"}
    assert repository.list_coin_purchase_grades() == []
    assert [(c.country,) for c in _combos(country="英国")] == []


def test_grades_delete_removes_manual_combo_without_orders(web):
    """手工新增的组合（无历史订单）也能整行删掉。"""
    _seed_orders()
    repository.upsert_coin_purchase_grade("英国", "1910", "1 便士", "好")
    orders_before = len(repository.list_coin_order_keys())

    resp = web.post(
        "/coins/grades/delete",
        data={"country": "英国", "era": "1910", "denomination": "1 便士"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert repository.list_coin_purchase_grades() == []
    assert len(repository.list_coin_order_keys()) == orders_before  # 订单一条没动


def test_grades_delete_validates_input(web):
    _seed_orders()
    resp = web.post(
        "/coins/grades/delete",
        data={"country": "英国", "era": "  ", "denomination": "1 便士"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert len(repository.list_coin_order_keys()) == 5

"""铜币页面测试：专场列表 / 拍品页 / 画像编辑 / 反爬提示（M7、M9，plan §18.8/§18.11）。

运行方式：`.venv/bin/python -m pytest tests/test_coins_pages.py -q`
走真实 lifespan（建库 + 同步源清单），但调度与启动补拉走空壳，不发真实网络请求。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, repository
from app.main import app as fastapi_app
from app.models import CoinFetchRun, CoinLot, CoinPriceRef, CoinSession, CoinShop


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


# ---------------------------------------------------------------- 反爬提示


def test_sessions_page_shows_blocked_notice(web):
    _seed_session()
    now = repository.utcnow()
    repository.record_coin_fetch_run(
        CoinFetchRun(
            id=None,
            kind="sessions",
            target="/auction",
            started_at=now,
            finished_at=now,
            status="blocked",
            http_status=200,
            items_new=0,
            error="captchaV2",
        )
    )

    page = web.get("/coins")
    assert "会话可能已失效" in page.text
    assert "coins-fetch --headful" in page.text


def test_sessions_page_shows_login_notice_for_price_errors(web):
    """定价因「要登录」失败时页面要提示去登录 —— 不能被收尾那条 ok 记录盖掉。"""
    now = repository.utcnow()
    repository.record_coin_fetch_run(
        CoinFetchRun(
            id=None,
            kind="price_search",
            target="英国 1919",
            started_at=now,
            finished_at=now,
            status="parse_error",
            http_status=None,
            items_new=0,
            error="成交价接口报错：登录后查看成交价",
        )
    )
    # 采集收尾又记了一条 ok（真实管道就是这样）—— 提示仍应在
    repository.record_coin_fetch_run(
        CoinFetchRun(
            id=None,
            kind="sessions",
            target="/auction",
            started_at=now,
            finished_at=now,
            status="ok",
            http_status=None,
            items_new=1,
            error=None,
        )
    )

    page = web.get("/coins")
    assert page.status_code == 200
    assert "成交价要登录" in page.text

    # 登录成功后再查一次价 → 提示要消失（只看最近一次查价，不翻旧账）
    repository.record_coin_fetch_run(
        CoinFetchRun(
            id=None,
            kind="price_search",
            target="英国 1919",
            started_at=now,
            finished_at=now,
            status="ok",
            http_status=None,
            items_new=3,
            error=None,
        )
    )
    assert "成交价要登录" not in web.get("/coins").text


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

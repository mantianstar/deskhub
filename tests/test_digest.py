"""日报与点击：排序、跨日边界、点击幂等、空数据态（plan §13.1 用例 8/9/10）。

运行方式：`.venv/bin/python -m pytest tests/test_digest.py -q`
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import config, db, fetcher, repository
from app.config import SourceConfig
from app.main import app as fastapi_app
from app.models import Item

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAY1 = ("2026-09-22T00:00:00Z", "2026-09-23T00:00:00Z")


@pytest.fixture()
def temp_db(tmp_path):
    """每个用例一个独立库文件，避免用例间互相污染。"""
    db.init_db(tmp_path / "test.db")
    return tmp_path


@pytest.fixture()
def web(tmp_path, monkeypatch):
    """走一遍真实 lifespan（建库 + 同步源清单），但库文件落在临时目录。

    副作用：日志仍写真实的 `data/logs/deskhub.log`（`log_path` 没有环境变量覆盖口），
    换来的是「首页真的能被渲染出来」这条端到端证据。
    """
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
            summary="摘要",
            fetched_at=fetched_at,
        )
    )
    assert item_id is not None
    return item_id


def _score(item_id: int, score: float) -> None:
    repository.upsert_score(item_id, score, f"理由 {item_id}", "fake-model", "v1")


# ---------------------------------------------------------------- 排序


def test_digest_orders_scored_first_then_score_desc(temp_db):
    """有分在前、分高在前、未评分在末尾（plan §13.1 用例 8）。"""
    source_id = _seed_source()
    low = _add_item(
        source_id,
        "低分",
        url="https://example.com/low",
        fetched_at="2026-09-22T05:00:00Z",
        published_at="2026-09-22T04:00:00Z",
    )
    high_old = _add_item(
        source_id,
        "高分旧",
        url="https://example.com/high-old",
        fetched_at="2026-09-22T03:00:00Z",
        published_at="2026-09-20T04:00:00Z",
    )
    high_new = _add_item(
        source_id,
        "高分新",
        url="https://example.com/high-new",
        fetched_at="2026-09-22T04:00:00Z",
        published_at="2026-09-22T03:00:00Z",
    )
    unscored = _add_item(
        source_id,
        "未评分",
        url="https://example.com/unscored",
        fetched_at="2026-09-22T06:00:00Z",
        published_at="2026-09-22T05:00:00Z",
    )
    _score(low, 42)
    _score(high_old, 88)
    _score(high_new, 88)

    items = repository.query_digest(*DAY1, None, 8)

    assert [item.item_id for item in items] == [high_new, high_old, low, unscored]
    assert [item.title for item in items] == ["高分新", "高分旧", "低分", "未评分"]
    assert items[-1].score is None and items[-1].reason is None


def test_digest_ties_fall_back_to_newest_id(temp_db):
    """同分且发布时间也相同 → id 大的在前（排序的最后一道保险）。"""
    source_id = _seed_source()
    first = _add_item(
        source_id,
        "先入库",
        url="https://example.com/first",
        fetched_at="2026-09-22T02:00:00Z",
        published_at="2026-09-22T01:00:00Z",
    )
    second = _add_item(
        source_id,
        "后入库",
        url="https://example.com/second",
        fetched_at="2026-09-22T02:00:00Z",
        published_at="2026-09-22T01:00:00Z",
    )
    _score(first, 60)
    _score(second, 60)

    items = repository.query_digest(*DAY1, None, 8)

    assert [item.item_id for item in items] == [second, first]


def test_digest_filters_by_module_and_limit(temp_db):
    """模块筛选与条数上限（M5 入口视图复用同一查询）。"""
    source_id = _seed_source()
    agent_id = _add_item(
        source_id, "agent 条目", url="https://example.com/agent", fetched_at="2026-09-22T07:00:00Z"
    )
    bigdata_id = _add_item(
        source_id,
        "bigdata 条目",
        url="https://example.com/bigdata",
        fetched_at="2026-09-22T08:00:00Z",
        module="bigdata",
    )

    assert [item.item_id for item in repository.query_digest(*DAY1, "agent", 8)] == [agent_id]
    assert [item.item_id for item in repository.query_digest(*DAY1, "bigdata", 8)] == [bigdata_id]
    assert len(repository.query_digest(*DAY1, None, 1)) == 1


# ---------------------------------------------------------------- 跨日边界


def test_day_window_matches_shanghai_midnight(temp_db):
    """上海 09-22 的当天窗口 = UTC 09-21T16:00 ~ 09-22T16:00。"""
    start, end = repository.local_day_bounds(
        SHANGHAI, now=datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
    )
    assert (start, end) == ("2026-09-21T16:00:00Z", "2026-09-22T16:00:00Z")


def test_items_at_day_boundaries_split_into_two_digests(temp_db):
    """上海 00:00:00 与 23:59:59 抓到的条目分属不同日报（plan §13.1 用例 9）。"""
    source_id = _seed_source()
    midnight = _add_item(
        source_id,
        "当天第一秒",
        url="https://example.com/midnight",
        fetched_at="2026-09-21T16:00:00Z",
    )
    last_moment = _add_item(
        source_id,
        "当天最后一秒",
        url="https://example.com/last",
        fetched_at="2026-09-22T15:59:59Z",
    )
    next_midnight = _add_item(
        source_id,
        "次日第一秒",
        url="https://example.com/next",
        fetched_at="2026-09-22T16:00:00Z",
    )

    day1 = repository.local_day_bounds(
        SHANGHAI, now=datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
    )
    day2 = repository.local_day_bounds(
        SHANGHAI, now=datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)
    )

    assert {item.item_id for item in repository.query_digest(*day1, None, 8)} == {
        midnight,
        last_moment,
    }
    assert [item.item_id for item in repository.query_digest(*day2, None, 8)] == [next_midnight]


def test_lookback_bounds_covers_recent_window(temp_db):
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    start, end = repository.lookback_bounds(48, now=now)
    assert (start, end) == ("2026-09-20T12:00:00Z", "2026-09-22T12:00:00Z")


# ---------------------------------------------------------------- 点击


def test_mark_clicked_keeps_first_click(temp_db):
    """点两次同一链接，`clicked_at` 保持首次（plan §13.1 用例 10）。"""
    source_id = _seed_source()
    item_id = _add_item(
        source_id, "会被点的条目", url="https://example.com/click", fetched_at=repository.utcnow()
    )

    assert repository.mark_clicked(item_id) is True
    # 把首次点击时间改成一个可辨认的旧值，再点一次：不该被覆盖
    with db.connect() as conn, conn:
        conn.execute(
            "UPDATE items SET clicked_at = ? WHERE id = ?", ("2026-01-01T00:00:00Z", item_id)
        )
    assert repository.mark_clicked(item_id) is True

    with db.connect() as conn:
        row = conn.execute("SELECT clicked_at FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["clicked_at"] == "2026-01-01T00:00:00Z"


def test_missing_item_has_no_url_and_cannot_be_clicked(temp_db):
    assert repository.get_item_url(12345) is None
    assert repository.mark_clicked(12345) is False


# ---------------------------------------------------------------- 首页渲染


def test_home_page_renders_cards_and_records_click(web):
    """首页能渲染出条目，外链走 /go/{id} 且被记录（spec §10 第 3 条）。"""
    source_id = repository.list_sources(only_enabled=True)[0].id
    item_id = _add_item(
        source_id, "今天的条目", url="https://example.com/today", fetched_at=repository.utcnow()
    )

    page = web.get("/")
    assert page.status_code == 200
    assert "今天的条目" in page.text
    assert f"/go/{item_id}" in page.text
    # 未评分的卡片显示文字而不是空白分数（plan §9.2）
    assert "未评分" in page.text
    assert web.get("/static/style.css").status_code == 200

    redirect = web.get(f"/go/{item_id}", follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"] == "https://example.com/today"
    with db.connect() as conn:
        clicked_at = conn.execute(
            "SELECT clicked_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()["clicked_at"]
    assert clicked_at is not None

    # 第二次点击仍然 302，但首次时间不变
    with db.connect() as conn, conn:
        conn.execute(
            "UPDATE items SET clicked_at = ? WHERE id = ?", ("2026-01-01T00:00:00Z", item_id)
        )
    assert web.get(f"/go/{item_id}", follow_redirects=False).status_code == 302
    with db.connect() as conn:
        again = conn.execute(
            "SELECT clicked_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()["clicked_at"]
    assert again == "2026-01-01T00:00:00Z"

    assert web.get("/go/999999", follow_redirects=False).status_code == 404


def test_home_page_says_first_fetch_running_when_db_empty(web):
    """清库后打开首页不报错，且说明是「正在抓」而不是「今天没内容」（M3-6）。"""
    page = web.get("/")
    assert page.status_code == 200
    assert "首次抓取进行中" in page.text


def test_home_page_falls_back_to_lookback_window_with_notice(web):
    """当日无候选时回退 48 小时并标注「非今日数据」（plan §9.1）。"""
    source_id = repository.list_sources(only_enabled=True)[0].id
    _add_item(
        source_id,
        "昨天抓到的条目",
        url="https://example.com/yesterday",
        fetched_at=repository.to_utc_str(datetime.now(timezone.utc) - timedelta(hours=24)),
    )

    page = web.get("/")
    assert page.status_code == 200
    assert "非今日数据" in page.text
    assert "昨天抓到的条目" in page.text

"""裸币判定与分类分析页（v22，plan §18.19.6 / §18.19.7）。

覆盖：
- v22 建列 / 升级 / 幂等，历史数据按标题**纯本地**回填；
- 采集映射 `lot_from_item` 写 `is_graded`，`upsert_coin_lot` 跟随标题覆盖；
- `/coins/analysis` 返回 200、默认只看硬币裸币、切筛选后数字变化、样本不足给占位。

运行方式：`.venv/bin/python -m pytest tests/test_coins_analysis.py -q`
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import config, db, repository
from app.coins import categories, gapi
from app.coins.analysis import MIN_SAMPLES, build_report
from app.coins.graded import is_graded_title
from app.main import app as fastapi_app
from app.models import CoinLot, CoinSession, CoinShop


# ---------------------------------------------------------------- 判定纯函数


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("1908年大清银币 PCGS MS62", 1),
        ("光绪元宝 公博评级 45", 1),
        ("NGC 高分银元", 1),
        ("保粹 盒装 铜元", 1),
        ("普通铜元 当十", 0),
        ("东印度公司1808年硬币", 0),
        ("", 0),
        (None, 0),
    ],
)
def test_is_graded_title(title, expected):
    assert is_graded_title(title) == expected


# ---------------------------------------------------------------- v22 迁移


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _version(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    return row["value"]


def test_fresh_db_has_is_graded_column(tmp_path):
    db.init_db(tmp_path / "fresh.db")

    with db.connect() as conn:
        assert "is_graded" in _columns(conn, "coin_lots")
        assert _version(conn) == "29"


def test_v21_db_upgrade_backfills_is_graded_locally(tmp_path):
    """v21 老库：ALTER 补列 + 按标题纯本地回填，不请求站点。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '21');
        CREATE TABLE coin_lots (
          id        INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER,
          mxiqi_id  TEXT NOT NULL UNIQUE,
          title     TEXT NOT NULL,
          match_score REAL NOT NULL DEFAULT 0,
          is_recommended INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO coin_lots (session_id, mxiqi_id, title) VALUES (1, 'a', 'PCGS 评级银元');
        INSERT INTO coin_lots (session_id, mxiqi_id, title) VALUES (1, 'b', '普通铜元');
        """
    )
    conn.commit()
    conn.close()

    db.init_db(path)

    with db.connect() as conn:
        rows = {
            row["mxiqi_id"]: row["is_graded"]
            for row in conn.execute("SELECT mxiqi_id, is_graded FROM coin_lots")
        }
        assert _version(conn) == "29"
    assert rows == {"a": 1, "b": 0}


def test_v22_upgrade_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '21');
        CREATE TABLE coin_lots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER,
          mxiqi_id TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL,
          match_score REAL NOT NULL DEFAULT 0,
          is_recommended INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()

    db.init_db(path)
    db.init_db(path)

    with db.connect() as conn:
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(coin_lots)")]
        assert columns.count("is_graded") == 1
        assert _version(conn) == "29"


def test_v27_adds_theme_columns_and_rebuilds_grade_index(tmp_path):
    """v26 老库：补「主题 / 名称」两列（旧行空串），唯一索引从 3 列换成 5 列，重复启动不报错。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '26');
        CREATE TABLE coin_purchase_grades (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          country      TEXT NOT NULL,
          era          TEXT NOT NULL,
          denomination TEXT NOT NULL,
          grade        TEXT NOT NULL,
          updated_at   TEXT NOT NULL
        );
        CREATE UNIQUE INDEX uidx_coin_purchase_grades
          ON coin_purchase_grades(country, era, denomination);
        INSERT INTO coin_purchase_grades (country, era, denomination, grade, updated_at)
          VALUES ('英国', '1910', '1 便士', '好', '2026-10-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    db.init_db(path)
    db.init_db(path)

    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(coin_purchase_grades)")}
        assert {"theme", "coin_name"} <= columns
        rows = [tuple(row) for row in conn.execute("SELECT theme, coin_name FROM coin_purchase_grades")]
        assert rows == [("", "")]
        index_columns = [
            row["name"]
            for row in conn.execute("PRAGMA index_info(uidx_coin_purchase_grades)")
        ]
        assert index_columns == ["country", "era", "denomination", "theme", "coin_name"]
        assert _version(conn) == "29"


def test_v28_pushes_theme_to_members_and_drops_series_columns(tmp_path):
    """v27 老库：系列级主题 / 名称复制给每个成员，然后删掉系列上的旧列；重复启动不报错。"""
    path = tmp_path / "v27.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '27');
        CREATE TABLE coin_collection_series (
          id             INTEGER PRIMARY KEY AUTOINCREMENT,
          collection_key TEXT NOT NULL UNIQUE,
          name           TEXT NOT NULL,
          country        TEXT NOT NULL,
          ruler          TEXT,
          denomination   TEXT NOT NULL,
          size_mm        REAL,
          size_band      TEXT,
          created_at     TEXT NOT NULL,
          updated_at     TEXT NOT NULL,
          theme          TEXT,
          coin_name      TEXT
        );
        CREATE TABLE coin_collection_series_members (
          id                  INTEGER PRIMARY KEY AUTOINCREMENT,
          collection_id       INTEGER NOT NULL REFERENCES coin_collection_series(id) ON DELETE CASCADE,
          numista_series_id   INTEGER NOT NULL UNIQUE,
          numista_series_name TEXT,
          created_at          TEXT NOT NULL
        );
        INSERT INTO coin_collection_series
          (collection_key, name, country, ruler, denomination, created_at, updated_at, theme, coin_name)
          VALUES ('cn-2015-5', '中国 · 5 元', '中国', NULL, '5 元', '2026-10-01', '2026-10-01', '亚运会', '女排');
        INSERT INTO coin_collection_series_members
          (collection_id, numista_series_id, numista_series_name, created_at)
          VALUES (1, 101, '女排', '2026-10-01'), (1, 102, '男排', '2026-10-01');
        """
    )
    conn.commit()
    conn.close()

    db.init_db(path)
    db.init_db(path)

    with db.connect() as conn:
        series_columns = _columns(conn, "coin_collection_series")
        assert "theme" not in series_columns
        assert "coin_name" not in series_columns
        assert {"theme", "coin_name"} <= _columns(conn, "coin_collection_series_members")
        rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT numista_series_id, theme, coin_name "
                "FROM coin_collection_series_members ORDER BY numista_series_id"
            )
        ]
        assert rows == [(101, "亚运会", "女排"), (102, "亚运会", "女排")]
        assert _version(conn) == "29"


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


def _session(mxiqi_id: str = "100") -> int:
    return repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id=mxiqi_id,
            shop_id=None,
            title=f"专场 {mxiqi_id}",
            status="closed",
            auction_at=None,
            auction_at_raw=None,
            deposit=None,
            lot_total=None,
            view_count=None,
            cover_urls=[],
            fetched_at="2026-10-01T00:00:00Z",
        )
    )


def test_lot_from_item_sets_is_graded():
    graded = gapi.lot_from_item({"item_id": 1, "name": "光绪元宝 NGC AU55"})
    raw = gapi.lot_from_item({"item_id": 2, "name": "普通铜元 当十"})

    assert graded is not None and graded.is_graded == 1
    assert raw is not None and raw.is_graded == 0


def test_upsert_coin_lot_follows_title_for_is_graded(temp_db):
    """标题每次重抓都会覆盖，`is_graded` 跟着标题走（不是 COALESCE）。"""
    session_id = _session()

    def _write(mxiqi_id: str, title: str) -> None:
        repository.upsert_coin_lot(
            CoinLot(
                id=None,
                session_id=session_id,
                mxiqi_id=mxiqi_id,
                lot_no=None,
                title=title,
                start_price=10.0,
                current_price=20.0,
                image_urls=[],
                tags={},
                match_score=0.0,
                is_recommended=False,
                fetched_at="2026-10-02T00:00:00Z",
                is_graded=is_graded_title(title),
            )
        )

    _write("100:1", "普通铜元")
    with db.connect() as conn:
        assert conn.execute(
            "SELECT is_graded FROM coin_lots WHERE mxiqi_id = ?", ("100:1",)
        ).fetchone()["is_graded"] == 0

    _write("100:1", "普通铜元 公博评级")
    with db.connect() as conn:
        assert conn.execute(
            "SELECT is_graded FROM coin_lots WHERE mxiqi_id = ?", ("100:1",)
        ).fetchone()["is_graded"] == 1


# ---------------------------------------------------------------- 分析页


@pytest.fixture()
def web(tmp_path, monkeypatch, no_scheduler):
    monkeypatch.setenv("DESKHUB_DB_PATH", str(tmp_path / "coins.db"))
    config.reset()
    with TestClient(fastapi_app) as client:
        yield client
    config.reset()


def _seed_analysis_data() -> None:
    """硬币两条二级分类 + 一条评级盒 + 一条外一级分类（钞票），供筛选断言。"""
    categories.sync_seed()
    now = repository.utcnow()
    shop_id = repository.upsert_coin_shop(
        CoinShop(id=None, mxiqi_uid="s1", name="测试小铺", avatar_url=None,
                 badges=[], fetched_at=now)
    )
    session_id = repository.upsert_coin_session(
        CoinSession(
            id=None, mxiqi_id="sess-1", shop_id=shop_id, title="专场", status="closed",
            auction_at="2026-10-01T00:00:00Z", auction_at_raw=None, deposit=None,
            lot_total=None, view_count=None, cover_urls=[], fetched_at=now,
        )
    )

    rows = [
        ("a", "普通铜元 当十", "11040", "sold", 120.0),
        ("b", "普通铜元 当二十", "11040", "sold", 80.0),
        ("c", "普通铜元 当百", "11040", "unsold", None),
        ("d", "古钱币 乾隆通宝", "11030", "sold", 60.0),
        ("e", "NGC 评级铜元", "11040", "sold", 500.0),
        ("f", "第一套人民币", "10000", "sold", 999.0),
    ]
    for mxiqi_id, title, code, outcome, final_price in rows:
        repository.upsert_coin_lot(
            CoinLot(
                id=None, session_id=session_id, mxiqi_id=f"sess-1:{mxiqi_id}",
                lot_no=None, title=title, start_price=10.0, current_price=final_price,
                image_urls=[], tags={}, match_score=0.0, is_recommended=False,
                fetched_at=now, outcome=outcome, final_price=final_price,
                closed_at=now if outcome else None, category_code=code,
                is_graded=is_graded_title(title),
            )
        )


def test_analysis_page_defaults_to_ungraded_coins(web):
    _seed_analysis_data()

    response = web.get("/coins/analysis")

    assert response.status_code == 200
    body = response.text
    # 默认口径：硬币 + 裸币 → 4 条（评级盒 1 条、钞票 1 条被排除）
    assert "4 件拍品" in body
    assert "NGC 评级铜元" not in body
    assert "第一套人民币" not in body
    # 样本 < 5 的分组必须给占位，而不是 0 / NaN
    assert "样本不足" in body


def test_analysis_page_category_filter_changes_numbers(web):
    _seed_analysis_data()

    all_coins = web.get("/coins/analysis").text
    only_copper = web.get("/coins/analysis?category=11040").text

    assert "4 件拍品" in all_coins
    assert "3 件拍品" in only_copper
    # 下拉里总有全部二级分类，这里断言的是分组表里不再出现古钱币那一行
    assert "乾隆通宝" not in only_copper


def test_analysis_page_graded_switch_includes_graded(web):
    _seed_analysis_data()

    response = web.get("/coins/analysis?graded=1")

    assert response.status_code == 200
    assert "5 件拍品" in response.text


def test_build_report_marks_small_groups_insufficient():
    """纯函数口径：样本不足不给分位数，成交率不含在拍。"""
    rows = [
        {"category_code": "11040", "category_name": "铜元", "outcome": "sold",
         "final_price": 100.0, "start_price": 10.0, "shop_id": 1, "shop_name": "店",
         "title": "甲", "numista_value": None, "id": 1, "session_id": 1,
         "auction_at": None, "closed_at": None, "is_graded": 0},
        {"category_code": "11040", "category_name": "铜元", "outcome": "running",
         "final_price": None, "start_price": 10.0, "shop_id": 1, "shop_name": "店",
         "title": "乙", "numista_value": None, "id": 2, "session_id": 1,
         "auction_at": None, "closed_at": None, "is_graded": 0},
    ]
    report = build_report(rows)

    assert report.totals == {"lots": 2, "sold": 1, "unsold": 0, "running": 1}
    group = report.overview[0]
    assert group["insufficient"] is True
    assert group["median"] is None
    assert group["rate"] == "100.0%"
    assert len(report.overview) == 1
    assert MIN_SAMPLES == 5

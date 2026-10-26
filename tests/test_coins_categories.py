"""拍品分类（v21）测试：建表 / 升级、分类字典、采集映射与历史回填
（plan §18.19.3 / §18.19.4 / §18.19.5）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from app import config as config_module
from app import db, repository
from app.coins import categories, gapi, pipeline
from app.coins.gapi import LotInfo, SessionCard, SessionDetail
from app.models import CoinLot, CoinSession


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


def _version(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    return row["value"]


def test_fresh_db_has_category_schema(tmp_path):
    path = tmp_path / "fresh.db"
    db.init_db(path)

    with db.connect() as conn:
        assert "category_code" in _columns(conn, "coin_lots")
        assert "coin_categories" in _tables(conn)
        assert _version(conn) == "22"


def test_v20_db_upgrade_adds_category_code(tmp_path):
    """老库（v20）没有 `category_code`：init_db 走 ALTER 补列，新表由 DDL 建。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '20');
        CREATE TABLE coin_lots (
          id    INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER,
          title TEXT NOT NULL,
          match_score REAL NOT NULL DEFAULT 0,
          is_recommended INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()

    db.init_db(path)

    with db.connect() as conn:
        assert "category_code" in _columns(conn, "coin_lots")
        assert "coin_categories" in _tables(conn)
        assert _version(conn) == "22"


def test_v21_upgrade_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '20');
        CREATE TABLE coin_lots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER,
          title TEXT NOT NULL,
          match_score REAL NOT NULL DEFAULT 0,
          is_recommended INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()

    db.init_db(path)
    db.init_db(path)  # 重复执行不报错，列也不会加两次

    with db.connect() as conn:
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(coin_lots)")]
        assert columns.count("category_code") == 1
        assert _version(conn) == "22"


# ---------------------------------------------------------------- 分类字典 / 采集映射


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


def _session(mxiqi_id: str, *, fetched_at: str = "2026-10-01T00:00:00Z") -> int:
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
            fetched_at=fetched_at,
        )
    )


def _lot(
    session_id: int,
    mxiqi_id: str,
    category_code: str | None = None,
    *,
    fetched_at: str = "2026-10-02T00:00:00Z",
) -> None:
    repository.upsert_coin_lot(
        CoinLot(
            id=None,
            session_id=session_id,
            mxiqi_id=mxiqi_id,
            lot_no=None,
            title="拍品",
            start_price=10.0,
            current_price=20.0,
            image_urls=[],
            tags={},
            match_score=0.0,
            is_recommended=False,
            fetched_at=fetched_at,
            category_code=category_code,
        )
    )


def _row(mxiqi_id: str) -> sqlite3.Row:
    with db.connect() as conn:
        return conn.execute(
            "SELECT * FROM coin_lots WHERE mxiqi_id = ?", (mxiqi_id,)
        ).fetchone()


def test_sync_seed_writes_dictionary(temp_db):
    categories.sync_seed()

    with db.connect() as conn:
        codes = {
            row["code"]
            for row in conn.execute("SELECT code FROM coin_categories")
        }
    assert {"10000", "11000", "11030"} <= codes
    assert categories.name_for("11030") == "古钱币"
    assert categories.parent_code("11030") == "11000"


def test_ensure_codes_falls_back_to_code_as_name(temp_db):
    """字典外的码不能挡外键：以 `name = code` 兜底落库（供回填后核对）。"""
    categories.sync_seed()

    categories.ensure_codes(["99999", None, "11030"])

    with db.connect() as conn:
        row = conn.execute(
            "SELECT name FROM coin_categories WHERE code = '99999'"
        ).fetchone()
    assert row["name"] == "99999"
    assert repository.coin_category_fallback_codes() == ["99999"]


def test_upsert_coin_lot_keeps_existing_category_when_new_is_none(temp_db):
    """COALESCE：详情缺分类（None）不擦库里已有的值。"""
    categories.sync_seed()
    session_id = _session("100")

    _lot(session_id, "100:1", "11030")
    _lot(session_id, "100:1", None)

    assert _row("100:1")["category_code"] == "11030"


def test_update_coin_lot_categories_writes_only_category_code(temp_db):
    """回填只改 `category_code`，标题 / 价格 / fetched_at 原样保留。"""
    categories.sync_seed()
    session_id = _session("100")
    _lot(session_id, "100:1")
    before = _row("100:1")

    written = repository.update_coin_lot_categories([("100:1", "11040")])

    after = _row("100:1")
    assert written == 1
    assert after["category_code"] == "11040"
    for column in ("title", "start_price", "current_price", "fetched_at", "session_id"):
        assert after[column] == before[column]


def test_list_coin_sessions_pending_category_skips_covered(temp_db):
    """已全覆盖的场次不进待补列表；其余按 `fetched_at` 升序。"""
    categories.sync_seed()
    late = _session("200", fetched_at="2026-10-05T00:00:00Z")
    early = _session("201", fetched_at="2026-10-01T00:00:00Z")
    covered = _session("202", fetched_at="2026-10-02T00:00:00Z")

    _lot(late, "200:1")
    _lot(early, "201:1")
    _lot(covered, "202:1", "11030")

    pending = repository.list_coin_sessions_pending_category("1970-01-01T00:00:00Z")

    assert [mxiqi_id for _session_id, mxiqi_id in pending] == ["201", "200"]
    assert repository.list_coin_sessions_pending_category(
        "2026-10-03T00:00:00Z"
    ) == [(late, "200")]
    assert repository.list_coin_sessions_pending_category("1970-01-01T00:00:00Z", 1) == [
        (early, "201")
    ]


class _DetailGapi:
    """只扮 `session_detail`：回填只需要专场详情这一种请求。"""

    def __init__(self, lots_by_session: dict[str, list[tuple[str, str | None]]]) -> None:
        self._lots = lots_by_session
        self.calls: list[str] = []

    async def session_detail(
        self, mxiqi_id: str, *, fallback: SessionCard | None = None
    ) -> SessionDetail:
        self.calls.append(mxiqi_id)
        return SessionDetail(
            card=SessionCard(
                mxiqi_id=mxiqi_id,
                title=f"专场 {mxiqi_id}",
                status="closed",
                auction_at_raw=None,
                lot_total=None,
                view_count=None,
                cover_urls=[],
            ),
            deposit=None,
            lots=[
                LotInfo(
                    mxiqi_id=lot_id,
                    lot_no=None,
                    title="拍品",
                    start_price=None,
                    current_price=None,
                    image_urls=[],
                    tags={},
                    category_code=code,
                )
                for lot_id, code in self._lots.get(mxiqi_id, [])
            ],
        )


def _cfg() -> config_module.Config:
    cfg = config_module.load()
    return replace(cfg, coins=replace(cfg.coins, request_interval_seconds=0))


@pytest.mark.asyncio
async def test_backfill_categories_skips_covered_and_writes_only_category(temp_db):
    categories.sync_seed()
    pending = _session("200", fetched_at="2026-10-01T00:00:00Z")
    covered = _session("201", fetched_at="2026-10-02T00:00:00Z")
    _lot(pending, "200:1")
    _lot(pending, "200:2", fetched_at="2026-10-02T00:00:00Z")
    _lot(covered, "201:1", "11030")

    api = _DetailGapi({"200": [("1", "11040"), ("2", None)]})

    result = await pipeline.backfill_categories(_cfg(), gapi=api)

    assert api.calls == ["200"]  # 已全覆盖的 201 不再请求
    assert result.sessions == 1
    assert result.lots_updated == 1  # 200:2 站点也不给分类，NULL 保持
    assert result.errors == 0
    assert _row("200:1")["category_code"] == "11040"
    assert _row("200:2")["category_code"] is None
    assert _row("200:1")["title"] == "拍品"  # 其他列不动

    coverage = repository.coin_lot_category_coverage()
    assert coverage == {"lots": 3, "categorized": 2, "uncategorized": 1}


@pytest.mark.asyncio
async def test_backfill_categories_records_error_and_continues(temp_db):
    categories.sync_seed()
    failing = _session("300", fetched_at="2026-10-01T00:00:00Z")
    covered = _session("301", fetched_at="2026-10-02T00:00:00Z")
    _lot(failing, "300:1")
    _lot(covered, "301:1", "11030")

    class _FailingGapi(_DetailGapi):
        async def session_detail(self, mxiqi_id: str, *, fallback=None):
            if mxiqi_id == "300":
                self.calls.append(mxiqi_id)
                raise gapi.GapiError("网关 502")
            return await super().session_detail(mxiqi_id, fallback=fallback)

    api = _FailingGapi({})

    result = await pipeline.backfill_categories(_cfg(), gapi=api)

    assert result.sessions == 0
    assert result.errors == 1
    assert api.calls == ["300"]
    with db.connect() as conn:
        kinds = [
            row["status"]
            for row in conn.execute(
                "SELECT status FROM coin_fetch_runs WHERE kind = ?", (pipeline.RUN_KIND_CATEGORY,)
            )
        ]
    assert kinds == ["http_error"]

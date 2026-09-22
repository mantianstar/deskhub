"""数据访问层：所有 SQL 只允许出现在这里。

入参与出参统一为 dataclass/dict，不向上层暴露 sqlite3.Row。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app import db, models
from app.config import SourceConfig
from app.models import FetchRun, FetchStatus, Item, ScoreCandidate, Source

# 连续失败达到该次数即在界面标红（plan §7.3）
FAIL_COUNT_RED_THRESHOLD = 3


# ---------------------------------------------------------------- 事务工具


@contextmanager
def _write_conn(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """写操作统一入口。

    不传 `conn` 时自开连接并包一个事务；传 `conn` 时直接复用调用方的事务 ——
    `save_fetch_result()` 靠这一点把多个写操作合成一次提交（plan §6.3）。
    """
    if conn is not None:
        yield conn
        return
    with db.connect() as owned, owned:
        yield owned


# ---------------------------------------------------------------- 时间工具


def utcnow() -> str:
    """当前 UTC 时间的存储格式：`2026-09-21T03:12:00Z`。

    禁止各模块自己调 `datetime.now()`，否则时间口径会漂。
    """
    return to_utc_str(datetime.now(timezone.utc))


def to_utc_str(value: datetime) -> str:
    """datetime → UTC ISO8601 字符串（秒精度）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(value: str | None) -> datetime | None:
    """存储字符串 → 带 UTC 时区的 datetime；无法解析返回 None。"""
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- 源

_SOURCE_COLUMNS = "id, name, url, module, enabled, last_ok_at, fail_count, created_at"


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        module=row["module"],
        enabled=bool(row["enabled"]),
        last_ok_at=row["last_ok_at"],
        fail_count=row["fail_count"],
        created_at=row["created_at"],
    )


def sync_sources_from_config(sources: tuple[SourceConfig, ...]) -> None:
    """把 YAML 源清单 upsert 进 `sources`。

    以 url 为匹配键：已存在的行保留 `last_ok_at` / `fail_count` / `created_at`（运行时状态），
    但 `name` / `module` / `enabled` 以配置文件为准 —— 配置是源清单的唯一真源（plan §16），
    界面上临时改的启停会在下次启动时被配置覆盖。

    配置里已不存在的源**只置 `enabled = 0`，不删行**：删行会级联带走 `items` 与 `fetch_runs`
    的历史，而「这个源曾经抓到过什么」本身就是产品信息（plan §16）。
    """
    now = utcnow()
    urls = [s.url for s in sources]
    placeholders = ", ".join("?" * len(urls))
    with db.connect() as conn, conn:
        conn.executemany(
            """
            INSERT INTO sources (name, url, module, enabled, fail_count, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
            ON CONFLICT(url) DO UPDATE SET
                name    = excluded.name,
                module  = excluded.module,
                enabled = excluded.enabled
            """,
            [(s.name, s.url, s.module, int(s.enabled), now) for s in sources],
        )
        conn.execute(
            f"UPDATE sources SET enabled = 0 WHERE url NOT IN ({placeholders})",
            urls,
        )


def list_sources(only_enabled: bool = False) -> list[Source]:
    """源列表，按 module、name 排序。"""
    sql = f"SELECT {_SOURCE_COLUMNS} FROM sources"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY module, name"
    with db.connect() as conn:
        return [_row_to_source(row) for row in conn.execute(sql)]


def set_source_enabled(source_id: int, enabled: bool) -> None:
    """启停开关。"""
    with db.connect() as conn, conn:
        conn.execute("UPDATE sources SET enabled = ? WHERE id = ?", (int(enabled), source_id))


def count_sources() -> int:
    with db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"])


def count_failing_sources() -> int:
    """连续失败达到标红阈值的源数量。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE fail_count >= ?",
            (FAIL_COUNT_RED_THRESHOLD,),
        ).fetchone()
        return int(row["n"])


# ---------------------------------------------------------------- 条目


_ITEM_COLUMNS = "source_id, module, title, url, url_hash, published_at, summary, fetched_at"


def insert_item_if_new(item: Item, *, conn: sqlite3.Connection | None = None) -> int | None:
    """插入条目；`url_hash` 冲突说明这条已经抓过，返回 None（幂等靠 UNIQUE 约束）。"""
    with _write_conn(conn) as active:
        cursor = active.execute(
            f"INSERT INTO items ({_ITEM_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url_hash) DO NOTHING",
            (
                item.source_id,
                item.module,
                item.title,
                item.url,
                item.url_hash,
                item.published_at,
                item.summary,
                item.fetched_at,
            ),
        )
        return int(cursor.lastrowid) if cursor.rowcount else None


# ---------------------------------------------------------------- 抓取日志与源状态


def record_fetch_run(run: FetchRun, *, conn: sqlite3.Connection | None = None) -> int:
    """写一条抓取日志，返回 fetch_runs.id。"""
    with _write_conn(conn) as active:
        cursor = active.execute(
            """
            INSERT INTO fetch_runs
                (source_id, started_at, finished_at, status, http_status, items_new, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.source_id,
                run.started_at,
                run.finished_at,
                run.status,
                run.http_status,
                run.items_new,
                run.error,
            ),
        )
        return int(cursor.lastrowid)


def mark_source_ok(
    source_id: int,
    at: str,
    *,
    reset_fail_count: bool = True,
    conn: sqlite3.Connection | None = None,
) -> None:
    """源本次抓取成功。

    `empty`（解析正常但 0 条）也算源是活的，更新 `last_ok_at` 但**不清零** `fail_count`
    —— 长期无更新的源不该被误标红，也不该靠一次空响应洗掉历史失败（plan §7.3）。
    """
    if reset_fail_count:
        sql = "UPDATE sources SET last_ok_at = ?, fail_count = 0 WHERE id = ?"
    else:
        sql = "UPDATE sources SET last_ok_at = ? WHERE id = ?"
    with _write_conn(conn) as active:
        active.execute(sql, (at, source_id))


def mark_source_fail(source_id: int, *, conn: sqlite3.Connection | None = None) -> None:
    """源本次抓取失败：`fail_count += 1`，`last_ok_at` 保持不变。"""
    with _write_conn(conn) as active:
        active.execute("UPDATE sources SET fail_count = fail_count + 1 WHERE id = ?", (source_id,))


def save_fetch_result(source_id: int, items: Sequence[Item], run: FetchRun) -> int:
    """一次抓取的结果在**同一事务**里落库：items + fetch_runs + 源状态，返回新增条目数。

    分三条 SQL 但共用一个连接，任何一步失败整轮回滚，不会出现「条目进了但日志没写」。
    """
    with _write_conn() as conn:
        new_ids = [insert_item_if_new(item, conn=conn) for item in items]
        run.items_new = sum(1 for item_id in new_ids if item_id is not None)
        record_fetch_run(run, conn=conn)

        ok_at = run.finished_at or run.started_at
        if run.status == FetchStatus.OK:
            mark_source_ok(source_id, ok_at, conn=conn)
        elif run.status == FetchStatus.EMPTY:
            mark_source_ok(source_id, ok_at, reset_fail_count=False, conn=conn)
        else:
            mark_source_fail(source_id, conn=conn)
        return run.items_new


def last_fetch_at() -> str | None:
    """最近一次抓取开始时间；从未抓取过返回 None。"""
    with db.connect() as conn:
        row = conn.execute("SELECT MAX(started_at) AS ts FROM fetch_runs").fetchone()
        return row["ts"]


# ---------------------------------------------------------------- 打分

# 待打分池的回溯窗口（plan §8.4）：再老的条目不再送模型，避免重扫历史烧 token
SCORE_LOOKBACK_DAYS = 7


def list_pending_score_items(
    limit: int,
    lookback_days: int = SCORE_LOOKBACK_DAYS,
) -> list[ScoreCandidate]:
    """待打分池：近 `lookback_days` 天抓进来、`item_scores` 里没有记录的条目。

    `LEFT JOIN ... WHERE item_id IS NULL` 即「没打过分」；打分失败的条目不会被写库，
    所以下一轮自然又落回这个池子里重试（plan §8.3）。
    """
    cutoff = to_utc_str(datetime.now(timezone.utc) - timedelta(days=lookback_days))
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT i.id AS item_id, i.module, i.title, i.summary, i.published_at,
                   s.name AS source_name
            FROM items i
            JOIN sources s ON s.id = i.source_id
            LEFT JOIN item_scores sc ON sc.item_id = i.id
            WHERE sc.item_id IS NULL
              AND i.fetched_at >= ?
            ORDER BY i.fetched_at DESC, i.id DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return [
        ScoreCandidate(
            item_id=row["item_id"],
            module=row["module"],
            title=row["title"],
            summary=row["summary"],
            published_at=row["published_at"],
            source_name=row["source_name"],
        )
        for row in rows
    ]


def upsert_score(
    item_id: int,
    score: float,
    reason: str,
    model: str | None,
    prompt_version: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    """打分落库；同一条目重跑就覆盖（`item_id` 是主键，天然不产生重复行）。"""
    with _write_conn(conn) as active:
        active.execute(
            """
            INSERT INTO item_scores (item_id, score, reason, model, prompt_version, scored_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                score          = excluded.score,
                reason         = excluded.reason,
                model          = excluded.model,
                prompt_version = excluded.prompt_version,
                scored_at      = excluded.scored_at
            """,
            (item_id, score, reason, model, prompt_version, utcnow()),
        )


def delete_scores_by_prompt_version(prompt_version: str) -> int:
    """删掉某个 prompt 版本的全部打分（`cli score --rescore` 用），返回删除行数。

    改 prompt 必须递增 `prompt_version`，否则新旧理由混在一张表里没法对比（plan §8.4）。
    """
    with db.connect() as conn, conn:
        cursor = conn.execute(
            "DELETE FROM item_scores WHERE prompt_version = ?", (prompt_version,)
        )
        return cursor.rowcount


def count_scored_items() -> int:
    with db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM item_scores").fetchone()["n"])


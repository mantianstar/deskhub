"""数据访问层：所有 SQL 只允许出现在这里。

入参与出参统一为 dataclass/dict，不向上层暴露 sqlite3.Row。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import db, models, textmatch
from app.coins import grades, translate
from app.config import SourceConfig
from app.models import (
    CoinBestLot,
    CoinCollectionMember,
    CoinCollectionSeries,
    CoinExploreCandidate,
    CoinFetchRun,
    CoinIngestTask,
    CoinIssue,
    CoinIssueValues,
    CoinLot,
    CoinLotCard,
    CoinOrder,
    CoinPageExploreItem,
    CoinPriceRef,
    CoinProfileTag,
    CoinPurchaseGrade,
    CoinSale,
    CoinSearchLot,
    CoinSeries,
    CoinSeriesIssueStats,
    CoinSession,
    CoinSessionCard,
    CoinSessionDetail,
    CoinShop,
    CoinTodayLot,
    CoinWatchItem,
    DigestItem,
    FetchRun,
    FetchStatus,
    Item,
    ScoreCandidate,
    Source,
)

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


BEIJING = ZoneInfo("Asia/Shanghai")


def now_local() -> str:
    """当前北京时间的存储格式：`2026-09-21T11:12:00`（纯墙上时间，无时区后缀）。

    禁止各模块自己调 `datetime.now()`，否则时间口径会漂。
    """
    return to_local_str(datetime.now(BEIJING))


def to_local_str(value: datetime) -> str:
    """datetime → 北京时间 ISO8601 字符串（秒精度、无后缀）。"""
    if value.tzinfo is not None:
        value = value.astimezone(BEIJING)
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def parse_dt(value: str | None) -> datetime | None:
    """存储字符串 → naive 的北京时间；无法解析返回 None。

    兼容旧版带 `Z` 的 UTC 串：读到就换算成北京时间再摘掉时区。
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(BEIJING).replace(tzinfo=None)


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
    now = now_local()
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


def toggle_source_enabled(source_id: int) -> bool | None:
    """启停取反，返回新状态；源不存在返回 None（`/sources/{id}/toggle` 据此判 404）。"""
    with db.connect() as conn, conn:
        row = conn.execute("SELECT enabled FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            return None
        new_value = not bool(row["enabled"])
        conn.execute("UPDATE sources SET enabled = ? WHERE id = ?", (int(new_value), source_id))
        return new_value


# ---------------------------------------------------------------- 功能开关（管理页 /control）


def feature_switch_states() -> dict[str, bool]:
    """已显式改过的功能开关；没有记录的功能由 `app/switches.py` 用默认值兜底。"""
    with db.connect() as conn:
        rows = conn.execute("SELECT key, enabled FROM feature_switches").fetchall()
    return {row["key"]: bool(row["enabled"]) for row in rows}


def set_feature_switch(key: str, enabled: bool) -> None:
    """写入一个功能开关（不存在则插入）。"""
    with db.connect() as conn, conn:
        conn.execute(
            """
            INSERT INTO feature_switches (key, enabled, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET enabled = excluded.enabled,
                                           updated_at = excluded.updated_at
            """,
            (key, int(enabled), now_local()),
        )


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


def _row_to_fetch_run(row: sqlite3.Row) -> FetchRun:
    return FetchRun(
        id=row["id"],
        source_id=row["source_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        http_status=row["http_status"],
        items_new=row["items_new"],
        error=row["error"],
    )


def latest_fetch_runs() -> dict[int, FetchRun]:
    """每个源最近一条 `fetch_runs`（按 id 最大），供源管理页显示最后一次的失败原因。

    没有抓取记录的源不会出现在结果里，调用方用 `.get()` 兜底。
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.source_id, r.started_at, r.finished_at, r.status,
                   r.http_status, r.items_new, r.error
            FROM fetch_runs r
            JOIN (SELECT source_id, MAX(id) AS last_id FROM fetch_runs GROUP BY source_id) t
              ON t.last_id = r.id
            """
        ).fetchall()
    return {row["source_id"]: _row_to_fetch_run(row) for row in rows}


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
    cutoff = to_local_str(datetime.now(timezone.utc) - timedelta(days=lookback_days))
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
            (item_id, score, reason, model, prompt_version, now_local()),
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


# ---------------------------------------------------------------- 日报


def day_bounds_local(tz: ZoneInfo, day: date) -> tuple[str, str]:
    """业务时区「某一天」的 `[start, end)`，直接是北京时间存储格式。

    返回左闭右开区间，故上海 00:00:00 与 23:59:59 抓到的条目必然分属不同区间。
    """
    day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return to_local_str(day_start), to_local_str(day_start + timedelta(days=1))


def local_day_bounds(tz: ZoneInfo, *, now: datetime | None = None) -> tuple[str, str]:
    """算出业务时区「今天」的 `[start, end)`，直接是北京时间存储格式（plan §9.1）。

    候选池用 `fetched_at` 而不是 `published_at`：日报要回答的是「**今天**新到的东西里
    哪些值得看」，用发布时间会漏掉「今天抓到的旧发布内容」（plan §12 第 7 条）。

    `now` 仅测试注入用。
    """
    moment = now or datetime.now(tz)
    if moment.tzinfo is not None:
        moment = moment.astimezone(tz)
    return day_bounds_local(tz, moment.date())


def lookback_bounds(hours: int, *, now: datetime | None = None) -> tuple[str, str]:
    """当日无候选时的回退窗口：最近 `hours` 小时（页面需标注「非今日数据」）。"""
    moment = now or datetime.now(BEIJING)
    return to_local_str(moment - timedelta(hours=hours)), to_local_str(moment)


def local_month_start(tz: ZoneInfo, *, now: datetime | None = None) -> str:
    """业务时区「本月 1 日 00:00」的北京时间存储格式（Numista 月度额度按此切月）。"""
    moment = now or datetime.now(tz)
    if moment.tzinfo is not None:
        moment = moment.astimezone(tz)
    return to_local_str(datetime(moment.year, moment.month, 1, tzinfo=tz))


_DIGEST_COLUMNS = """i.id AS item_id, i.module, i.title, i.url, i.published_at, i.fetched_at,
               s.name AS source_name, sc.score AS score, sc.reason AS reason"""


def _row_to_digest_item(row: sqlite3.Row) -> DigestItem:
    """items + 源名 + 打分 → 列表卡片对象（`query_digest` 与 `search_items` 共用）。"""
    return DigestItem(
        item_id=row["item_id"],
        module=row["module"],
        title=row["title"],
        url=row["url"],
        source_name=row["source_name"],
        published_at=row["published_at"],
        fetched_at=row["fetched_at"],
        score=row["score"],
        reason=row["reason"],
    )


def query_digest(
    day_start_utc: str,
    day_end_utc: str,
    module: str | None = None,
    limit: int = 8,
) -> list[DigestItem]:
    """日报查询：窗口内的条目按「分数优先」排序取前 `limit` 条（plan §9.2）。

    排序即 `(score IS NULL) ASC, score DESC, published_at DESC, id DESC`：
    有分的在前、分高在前、并列时新的在前、未打分的落在末尾（`published_at` 为 NULL 的
    —— 如美团 feed —— 也自然排在后面）。
    """
    sql = f"""
        SELECT {_DIGEST_COLUMNS}
        FROM items i
        JOIN sources s ON s.id = i.source_id
        LEFT JOIN item_scores sc ON sc.item_id = i.id
        WHERE i.fetched_at >= ? AND i.fetched_at < ?
    """
    params: list[object] = [day_start_utc, day_end_utc]
    if module is not None:
        sql += " AND i.module = ?"
        params.append(module)
    sql += """
        ORDER BY (sc.score IS NULL), sc.score DESC, i.published_at DESC, i.id DESC
        LIMIT ?
    """
    params.append(limit)

    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_digest_item(row) for row in rows]


def get_item_url(item_id: int) -> str | None:
    """条目的原始链接；不存在返回 None（`/go/{id}` 据此返回 404）。"""
    with db.connect() as conn:
        row = conn.execute("SELECT url FROM items WHERE id = ?", (item_id,)).fetchone()
    return row["url"] if row else None


def mark_clicked(item_id: int) -> bool:
    """记录点击：`COALESCE` 保留**首次**点击时间，重复点击不会覆盖它（plan §9.3）。

    返回条目是否存在（`UPDATE` 的 rowcount 反映匹配行数，与值是否变化无关）。
    """
    with db.connect() as conn, conn:
        cursor = conn.execute(
            "UPDATE items SET clicked_at = COALESCE(clicked_at, ?) WHERE id = ?",
            (now_local(), item_id),
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------------- 历史搜索

# 搜索一页的条数；上限防止手改 url 后一次拉爆整库
SEARCH_PAGE_SIZE = 20
SEARCH_MAX_PAGE_SIZE = 100


def search_items(
    q: str | None = None,
    module: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    page: int = 1,
    page_size: int = SEARCH_PAGE_SIZE,
) -> tuple[list[DigestItem], int]:
    """历史搜索：标题 + 摘要 LIKE，按发布时间倒序分页，返回（本页条目, 命中总数）。

    时间范围（`start_utc` / `end_utc`）与排序都用 `published_at`：卡片上显示的就是发布时间，
    页面上「筛什么」与「看到什么」必须是同一个口径（日报候选池用 `fetched_at` 是另一回事，
    它回答的是「今天新到的东西」，见 plan §12 第 7 条）。

    代价：`published_at` 为 NULL 的条目（源不给发布时间，如美团 feed）在**设了时间范围**时
    会被排除 —— 它们没有可筛的发布时间。不设时间范围时照常能搜到。这个取舍见 plan §12 第 13 条。
    """
    conditions: list[str] = []
    params: list[object] = []
    keyword = (q or "").strip()
    if keyword:
        like = f"%{keyword}%"
        conditions.append("(i.title LIKE ? OR i.summary LIKE ?)")
        params.extend([like, like])
    if module:
        conditions.append("i.module = ?")
        params.append(module)
    if start_utc:
        conditions.append("i.published_at >= ?")
        params.append(start_utc)
    if end_utc:
        conditions.append("i.published_at < ?")
        params.append(end_utc)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    safe_page = max(1, page)
    safe_page_size = min(max(1, page_size), SEARCH_MAX_PAGE_SIZE)
    with db.connect() as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) AS n FROM items i {where}", params).fetchone()["n"]
        )
        rows = conn.execute(
            f"""
            SELECT {_DIGEST_COLUMNS}
            FROM items i
            JOIN sources s ON s.id = i.source_id
            LEFT JOIN item_scores sc ON sc.item_id = i.id
            {where}
            ORDER BY i.published_at DESC, i.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, safe_page_size, (safe_page - 1) * safe_page_size],
        ).fetchall()
    return [_row_to_digest_item(row) for row in rows], total


# ---------------------------------------------------------------- 清理


def count_items_before(cutoff_utc: str) -> int:
    """`fetched_at < cutoff_utc` 的条目数（`cli purge` 不带 `--yes` 时预览用）。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE fetched_at < ?", (cutoff_utc,)
        ).fetchone()
    return int(row["n"])


def purge_items_before(cutoff_utc: str) -> int:
    """删除 `fetched_at < cutoff_utc` 的条目，返回删除条数。

    只动 `items`：`item_scores` 靠 `ON DELETE CASCADE` 一起走；`sources` / `fetch_runs`
    保留 —— 清掉的是内容，不是「源的状态与历史」（plan §11.3 / §16）。
    """
    with db.connect() as conn, conn:
        cursor = conn.execute("DELETE FROM items WHERE fetched_at < ?", (cutoff_utc,))
        return cursor.rowcount


# ================================================================ 铜币拍卖
# 全部 SQL 集中在这里；`app/coins/*` 只调这些函数（layering 同知识聚合，plan §3.2）。


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _json_dict(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------- 订单与画像


def upsert_coin_order(order: CoinOrder) -> None:
    """写一条画像原料；`(order_no, item_name)` 冲突则覆盖抽取字段（幂等重跑）。"""
    with db.connect() as conn, conn:
        conn.execute(
            """
            INSERT INTO coin_orders
                (order_no, shop_name, item_name, price,
                 country, era, denomination, category, condition, parsed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_no, item_name) DO UPDATE SET
                shop_name    = excluded.shop_name,
                price        = excluded.price,
                country      = excluded.country,
                era          = excluded.era,
                denomination = excluded.denomination,
                category     = excluded.category,
                condition    = excluded.condition,
                parsed_at    = excluded.parsed_at
            """,
            (
                order.order_no,
                order.shop_name,
                order.item_name,
                order.price,
                order.country,
                order.era,
                order.denomination,
                order.category,
                order.condition,
                now_local(),
            ),
        )


def count_coin_orders() -> int:
    with db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM coin_orders").fetchone()["n"])


def list_coin_orders() -> list[CoinOrder]:
    """全部订单解析结果，供「不重新调 LLM 也能重算画像」。"""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT order_no, shop_name, item_name, price,
                   country, era, denomination, category, condition
            FROM coin_orders ORDER BY id
            """
        ).fetchall()
    return [
        CoinOrder(
            order_no=row["order_no"],
            shop_name=row["shop_name"],
            item_name=row["item_name"],
            price=row["price"],
            country=row["country"],
            era=row["era"],
            denomination=row["denomination"],
            category=row["category"],
            condition=row["condition"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------- 历史购买品相录入（spec §14.16）
# 去重 / 年份面值统一 / 合并一律在 `app.coins.grades`（纯函数）；这里只做读写。


def list_coin_purchase_grades() -> list[CoinPurchaseGrade]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, country, era, denomination, grade, updated_at, theme, coin_name
            FROM coin_purchase_grades ORDER BY country, era, denomination
            """
        ).fetchall()
    return [
        CoinPurchaseGrade(
            id=row["id"],
            country=row["country"],
            era=row["era"],
            denomination=row["denomination"],
            grade=row["grade"],
            updated_at=row["updated_at"],
            theme=row["theme"] or "",
            coin_name=row["coin_name"] or "",
        )
        for row in rows
    ]


def upsert_coin_purchase_grade(
    country: str,
    era: str,
    denomination: str,
    grade: str,
    theme: str = "",
    coin_name: str = "",
) -> None:
    """按「国家 + 年份 + 面值 + 主题 + 名称」写一档品相（冲突则覆盖，幂等重存）。"""
    with db.connect() as conn, conn:
        conn.execute(
            """
            INSERT INTO coin_purchase_grades
                (country, era, denomination, grade, updated_at, theme, coin_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(country, era, denomination, theme, coin_name) DO UPDATE SET
                grade      = excluded.grade,
                updated_at = excluded.updated_at
            """,
            (country, era, denomination, grade, now_local(), theme, coin_name),
        )


def delete_coin_purchase_grade(
    country: str, era: str, denomination: str, theme: str = "", coin_name: str = ""
) -> bool:
    """清除某组合的品相（下拉选回「未录入」时用）。返回是否真的删了一行。"""
    with db.connect() as conn, conn:
        cursor = conn.execute(
            """
            DELETE FROM coin_purchase_grades
            WHERE country = ? AND era = ? AND denomination = ?
              AND theme = ? AND coin_name = ?
            """,
            (country, era, denomination, theme, coin_name),
        )
    return cursor.rowcount > 0


def list_coin_order_keys() -> list[tuple[int, str | None, str | None, str | None]]:
    """`(id, country, era, denomination)`：供品相页按**统一后的组合**反查要删的历史订单。

    存的是原始写法，比对必须走 `app.coins.grades`（不能按列相等）。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, country, era, denomination FROM coin_orders ORDER BY id"
        ).fetchall()
    return [(row["id"], row["country"], row["era"], row["denomination"]) for row in rows]


def delete_coin_orders(ids: Sequence[int]) -> int:
    """按 id 删除历史订单（品相页删「错误数据」整行时用）。返回实际删除的行数。"""
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with db.connect() as conn, conn:
        cursor = conn.execute(
            f"DELETE FROM coin_orders WHERE id IN ({placeholders})", list(ids)
        )
    return cursor.rowcount


# ---------------------------------------------------------------- 收藏系列（spec §14.17）


def _row_to_collection_series(row: sqlite3.Row) -> CoinCollectionSeries:
    return CoinCollectionSeries(
        id=row["id"],
        collection_key=row["collection_key"],
        name=row["name"],
        country=row["country"],
        ruler=row["ruler"],
        denomination=row["denomination"],
        size_mm=row["size_mm"],
        size_band=row["size_band"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_coin_collection_series() -> list[CoinCollectionSeries]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM coin_collection_series ORDER BY country, ruler, denomination, id"
        ).fetchall()
    return [_row_to_collection_series(row) for row in rows]


def get_coin_collection_series(collection_id: int) -> CoinCollectionSeries | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM coin_collection_series WHERE id = ?", (collection_id,)
        ).fetchone()
    return _row_to_collection_series(row) if row else None


def upsert_coin_collection_series(
    *,
    collection_key: str,
    name: str,
    country: str,
    ruler: str | None,
    denomination: str,
    size_mm: float | None = None,
    size_band: str | None = None,
) -> int:
    """按 `collection_key` 建/改收藏系列，返回 id。"""
    now = now_local()
    with db.connect() as conn, conn:
        conn.execute(
            """
            INSERT INTO coin_collection_series
                (collection_key, name, country, ruler, denomination, size_mm, size_band,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(collection_key) DO UPDATE SET
                name         = excluded.name,
                country      = excluded.country,
                ruler        = excluded.ruler,
                denomination = excluded.denomination,
                size_mm      = excluded.size_mm,
                size_band    = excluded.size_band,
                updated_at   = excluded.updated_at
            """,
            (
                collection_key,
                name,
                country,
                ruler,
                denomination,
                size_mm,
                size_band,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM coin_collection_series WHERE collection_key = ?", (collection_key,)
        ).fetchone()
    return int(row["id"])


def delete_coin_collection_series(collection_id: int) -> bool:
    with db.connect() as conn, conn:
        cursor = conn.execute("DELETE FROM coin_collection_series WHERE id = ?", (collection_id,))
    return cursor.rowcount > 0


def _row_to_collection_member(row: sqlite3.Row) -> CoinCollectionMember:
    return CoinCollectionMember(
        id=row["id"],
        collection_id=row["collection_id"],
        numista_series_id=row["numista_series_id"],
        numista_series_name=row["numista_series_name"],
        created_at=row["created_at"],
        theme=row["theme"] or "",
        coin_name=row["coin_name"] or "",
    )


def list_coin_collection_members(collection_id: int | None = None) -> list[CoinCollectionMember]:
    sql = "SELECT * FROM coin_collection_series_members"
    params: list[object] = []
    if collection_id is not None:
        sql += " WHERE collection_id = ?"
        params.append(collection_id)
    sql += " ORDER BY collection_id, numista_series_id"
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_collection_member(row) for row in rows]


def upsert_coin_collection_member(
    collection_id: int,
    numista_series_id: int,
    numista_series_name: str | None,
) -> None:
    """把一个 Numista 系列映射到收藏系列；已映射到别处则改归属（冲突覆盖）。"""
    with db.connect() as conn, conn:
        conn.execute(
            """
            INSERT INTO coin_collection_series_members
                (collection_id, numista_series_id, numista_series_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(numista_series_id) DO UPDATE SET
                collection_id       = excluded.collection_id,
                numista_series_name = excluded.numista_series_name
            """,
            (
                collection_id,
                numista_series_id,
                numista_series_name,
                now_local(),
            ),
        )


def update_coin_series_issue_fields(
    numista_id: int, *, issue_series: str, issue_theme: str
) -> bool:
    """保存系列详情页人工维护的发行系列 / 发行主题（英文表与中文表一起写）。"""
    with db.connect() as conn, conn:
        changed = 0
        for table in ("coin_series", "coin_series_cn"):
            cursor = conn.execute(
                f"UPDATE {table} SET issue_series = ?, issue_theme = ? WHERE numista_id = ?",
                (issue_series.strip(), issue_theme.strip(), numista_id),
            )
            changed += cursor.rowcount
    return changed > 0


def delete_coin_collection_member(member_id: int) -> bool:
    with db.connect() as conn, conn:
        cursor = conn.execute(
            "DELETE FROM coin_collection_series_members WHERE id = ?", (member_id,)
        )
    return cursor.rowcount > 0


# ---------------------------------------------------------------- 观察系列（spec §14.18）


def _row_to_watch_item(row: sqlite3.Row) -> CoinWatchItem:
    return CoinWatchItem(
        id=row["id"],
        series_id=row["series_id"],
        numista_id=row["numista_id"],
        title=row["title"],
        issuer=row["issuer"],
        ruler=row["ruler"],
        denomination=row["denomination"],
        min_year=row["min_year"],
        max_year=row["max_year"],
        url=row["url"],
        created_at=row["created_at"],
    )


def add_coin_watch_item(numista_id: int) -> bool:
    """加入观察（幂等）。系列不存在或已在观察返回 False。"""
    now = now_local()
    with db.connect() as conn, conn:
        row = conn.execute(
            "SELECT id FROM coin_series WHERE numista_id = ?", (numista_id,)
        ).fetchone()
        if row is None:
            return False
        cursor = conn.execute(
            "INSERT OR IGNORE INTO coin_watchlist (series_id, created_at) VALUES (?, ?)",
            (row["id"], now),
        )
    return cursor.rowcount > 0


def remove_coin_watch_item(numista_id: int) -> bool:
    with db.connect() as conn, conn:
        cursor = conn.execute(
            """
            DELETE FROM coin_watchlist
             WHERE series_id IN (SELECT id FROM coin_series WHERE numista_id = ?)
            """,
            (numista_id,),
        )
    return cursor.rowcount > 0


def list_coin_watchlist() -> list[CoinWatchItem]:
    """全部观察项（新加入在前）；展示字段优先取中文表。"""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.series_id, s.numista_id,
                   COALESCE(c.title, s.title)               AS title,
                   COALESCE(c.issuer, s.issuer)             AS issuer,
                   COALESCE(c.ruler, s.ruler)               AS ruler,
                   COALESCE(c.denomination, s.denomination) AS denomination,
                   COALESCE(c.min_year, s.min_year)         AS min_year,
                   COALESCE(c.max_year, s.max_year)         AS max_year,
                   COALESCE(c.url, s.url)                   AS url,
                   w.created_at
              FROM coin_watchlist AS w
              JOIN coin_series AS s ON s.id = w.series_id
              LEFT JOIN coin_series_cn AS c ON c.numista_id = s.numista_id
             ORDER BY w.created_at DESC, w.id DESC
            """
        ).fetchall()
    return [_row_to_watch_item(row) for row in rows]


# ---------------------------------------------------------------- 异步采集任务

INGEST_NODES = ("series", "years", "values", "images")
INGEST_NODE_STATES = ("wait", "running", "done", "skip", "failed")


def _row_to_ingest_task(row: sqlite3.Row) -> CoinIngestTask:
    return CoinIngestTask(
        id=row["id"],
        series_numista_id=row["series_numista_id"],
        name=row["name"],
        status=row["status"],
        node_series=row["node_series"],
        node_years=row["node_years"],
        node_values=row["node_values"],
        node_images=row["node_images"],
        source=row["source"],
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def enqueue_coin_ingest_task(
    series_numista_id: int, name: str = "", source: str = ""
) -> tuple[CoinIngestTask, bool]:
    """入队一个系列的采集任务；同系列已在 pending / running 时复用（reused=True）。"""
    now = now_local()
    with db.connect() as conn, conn:
        row = conn.execute(
            """
            SELECT * FROM coin_ingest_tasks
             WHERE series_numista_id = ? AND status IN ('pending', 'running')
             ORDER BY id LIMIT 1
            """,
            (series_numista_id,),
        ).fetchone()
        if row is not None:
            return _row_to_ingest_task(row), True
        cursor = conn.execute(
            """
            INSERT INTO coin_ingest_tasks (series_numista_id, name, source, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (series_numista_id, name, source, now),
        )
        row = conn.execute(
            "SELECT * FROM coin_ingest_tasks WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return _row_to_ingest_task(row), False


def list_coin_ingest_tasks(
    series_numista_id: int | None = None,
    name: str | None = None,
    status: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    finished_from: str | None = None,
    finished_to: str | None = None,
) -> list[CoinIngestTask]:
    """任务列表（新入队在前）；五组条件可组合：系列 ID / 任务名 / 状态 / 加入时间 / 完成时间。"""
    where: list[str] = []
    params: list[object] = []
    if series_numista_id is not None:
        where.append("series_numista_id = ?")
        params.append(series_numista_id)
    if name:
        where.append("name LIKE ?")
        params.append(f"%{name}%")
    if status:
        where.append("status = ?")
        params.append(status)
    if created_from:
        where.append("created_at >= ?")
        params.append(created_from)
    if created_to:
        where.append("created_at <= ?")
        params.append(created_to)
    if finished_from:
        where.append("finished_at >= ?")
        params.append(finished_from)
    if finished_to:
        where.append("finished_at <= ?")
        params.append(finished_to)
    sql = "SELECT * FROM coin_ingest_tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC"
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_ingest_task(row) for row in rows]


def delete_coin_ingest_task(task_id: int) -> bool:
    """按 ID 删除一条采集任务（从任务队列移除）。"""
    with db.connect() as conn, conn:
        cursor = conn.execute("DELETE FROM coin_ingest_tasks WHERE id = ?", (task_id,))
    return cursor.rowcount > 0


def set_coin_ingest_node(
    task_id: int, node: str, state: str, error: str | None = None
) -> bool:
    """推进单个节点状态；带 error 时同步记到任务（节点级 failed 的详情）。"""
    if node not in INGEST_NODES:
        raise ValueError(f"未知采集节点：{node}")
    if state not in INGEST_NODE_STATES:
        raise ValueError(f"未知节点状态：{state}")
    with db.connect() as conn, conn:
        cursor = conn.execute(
            f"UPDATE coin_ingest_tasks SET node_{node} = ?, error = COALESCE(?, error) WHERE id = ?",
            (state, error, task_id),
        )
    return cursor.rowcount > 0


def claim_next_coin_ingest_task() -> CoinIngestTask | None:
    """取最早入队的 pending 任务并置为 running（无任务返回 None）。"""
    now = now_local()
    with db.connect() as conn, conn:
        row = conn.execute(
            """
            SELECT id FROM coin_ingest_tasks
             WHERE status = 'pending'
             ORDER BY created_at, id LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE coin_ingest_tasks SET status = 'running', started_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        row = conn.execute(
            "SELECT * FROM coin_ingest_tasks WHERE id = ?", (row["id"],)
        ).fetchone()
    return _row_to_ingest_task(row)


def finish_coin_ingest_task(
    task_id: int, status: str, error: str | None = None
) -> bool:
    """收尾任务：done / failed + 完成时间。"""
    if status not in ("done", "failed"):
        raise ValueError(f"未知任务状态：{status}")
    now = now_local()
    with db.connect() as conn, conn:
        cursor = conn.execute(
            "UPDATE coin_ingest_tasks SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, error, now, task_id),
        )
    return cursor.rowcount > 0


def _row_to_explore_candidate(row: sqlite3.Row) -> CoinExploreCandidate:
    return CoinExploreCandidate(
        id=row["id"],
        numista_id=row["numista_id"],
        seed_key=row["seed_key"],
        seed_name=row["seed_name"],
        size_diff=row["size_diff"],
        status=row["status"],
        payload=json.loads(row["payload"]),
        payload_cn=json.loads(row["payload_cn"]),
        issues=json.loads(row["issues"]),
        verified_at=row["verified_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def upsert_coin_explore_candidate(candidate: CoinExploreCandidate) -> int:
    """按 numista_id 写入候选；**不改已有 status**（忽略过的不会被重新扫描复活）。"""
    now = now_local()
    with db.connect() as conn, conn:
        conn.execute(
            """
            INSERT INTO coin_explore_candidates
                (numista_id, seed_key, seed_name, size_diff, status, payload, payload_cn,
                 issues, verified_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(numista_id) DO UPDATE SET
                seed_key    = excluded.seed_key,
                seed_name   = excluded.seed_name,
                size_diff   = excluded.size_diff,
                payload     = excluded.payload,
                payload_cn  = excluded.payload_cn,
                issues      = excluded.issues,
                verified_at = excluded.verified_at,
                updated_at  = excluded.updated_at
            """,
            (
                candidate.numista_id,
                candidate.seed_key,
                candidate.seed_name,
                candidate.size_diff,
                candidate.status,
                _dump(candidate.payload),
                _dump(candidate.payload_cn),
                _dump(candidate.issues),
                candidate.verified_at,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM coin_explore_candidates WHERE numista_id = ?",
            (candidate.numista_id,),
        ).fetchone()
    return int(row["id"])


def list_coin_explore_candidates(*, include_ignored: bool = False) -> list[CoinExploreCandidate]:
    sql = "SELECT * FROM coin_explore_candidates"
    if not include_ignored:
        sql += " WHERE status <> 'ignored'"
    sql += " ORDER BY CASE status WHEN 'candidate' THEN 0 ELSE 1 END, numista_id"
    with db.connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_explore_candidate(row) for row in rows]


def upsert_coin_page_explore_item(item: CoinPageExploreItem) -> bool:
    """按 numista_id 写入页面探索结果；已存在则跳过，返回是否新插入。"""
    now = now_local()
    with db.connect() as conn, conn:
        cursor = conn.execute(
            """
            INSERT INTO coin_page_explore_items
                (batch_id, numista_id, title, title_cn, issuer, issuer_cn, ruler, ruler_cn,
                 denomination, denomination_cn, year_text, min_year, max_year,
                 composition, composition_cn, size_mm, source_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(numista_id) DO NOTHING
            """,
            (
                item.batch_id,
                item.numista_id,
                item.title,
                item.title_cn,
                item.issuer,
                item.issuer_cn,
                item.ruler,
                item.ruler_cn,
                item.denomination,
                item.denomination_cn,
                item.year_text,
                item.min_year,
                item.max_year,
                item.composition,
                item.composition_cn,
                item.size_mm,
                item.source_url,
                now,
                now,
            ),
        )
        # 冲突跳过时行仍挂在老批次上，这里单独记一条成员关系，供按批次查询时用
        conn.execute(
            """
            INSERT OR IGNORE INTO coin_page_explore_batch_items (batch_id, numista_id)
            VALUES (?, ?)
            """,
            (item.batch_id, item.numista_id),
        )
        inserted = cursor.rowcount > 0
        if not inserted:
            # 行是展示缓存：翻译口径变了要跟着刷中文列，批次号保持不变
            conn.execute(
                """
                UPDATE coin_page_explore_items
                SET title_cn = ?, issuer_cn = ?, ruler_cn = ?, denomination_cn = ?,
                    composition_cn = ?, updated_at = ?
                WHERE numista_id = ?
                """,
                (
                    item.title_cn,
                    item.issuer_cn,
                    item.ruler_cn,
                    item.denomination_cn,
                    item.composition_cn,
                    now,
                    item.numista_id,
                ),
            )
        return inserted


def list_coin_page_explore_items(batch_id: str) -> list[CoinPageExploreItem]:
    """本批结果 = 行上 batch_id 命中 + 本批成员表命中（重复探索的 ID 只留在老行上）。"""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM coin_page_explore_items
            WHERE batch_id = ?
               OR numista_id IN (
                   SELECT numista_id FROM coin_page_explore_batch_items WHERE batch_id = ?
               )
            ORDER BY numista_id
            """,
            (batch_id, batch_id),
        ).fetchall()
    return [_row_to_page_explore_item(row) for row in rows]


def _row_to_page_explore_item(row) -> CoinPageExploreItem:
    return CoinPageExploreItem(
        numista_id=int(row["numista_id"]),
        batch_id=str(row["batch_id"]),
        title=row["title"],
        title_cn=row["title_cn"],
        issuer=row["issuer"],
        issuer_cn=row["issuer_cn"],
        ruler=row["ruler"],
        ruler_cn=row["ruler_cn"],
        denomination=row["denomination"],
        denomination_cn=row["denomination_cn"],
        year_text=row["year_text"],
        min_year=row["min_year"],
        max_year=row["max_year"],
        composition=row["composition"],
        composition_cn=row["composition_cn"],
        size_mm=row["size_mm"],
        source_url=row["source_url"],
    )


def get_coin_explore_candidate(numista_id: int) -> CoinExploreCandidate | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM coin_explore_candidates WHERE numista_id = ?", (numista_id,)
        ).fetchone()
    return _row_to_explore_candidate(row) if row else None


def set_coin_explore_candidate_status(numista_id: int, status: str) -> bool:
    with db.connect() as conn, conn:
        cursor = conn.execute(
            "UPDATE coin_explore_candidates SET status = ?, updated_at = ? WHERE numista_id = ?",
            (status, now_local(), numista_id),
        )
    return cursor.rowcount > 0


def list_coin_today_lots(
    tz: ZoneInfo,
    *,
    now: datetime | None = None,
    category_prefixes: Sequence[str] | None = None,
    ungraded_only: bool = False,
    exclude_title_keywords: Sequence[str] | None = None,
) -> list[CoinTodayLot]:
    """当日截拍专场里的全部拍品（推荐匹配用）。

    传 `category_prefixes` 只取这些类目的拍品；`ungraded_only` 排除评级盒（`is_graded=1`）；
    `exclude_title_keywords` 排除标题含任一关键词的拍品。
    """
    start, end = local_day_bounds(tz, now=now)
    return _list_coin_lots_between(
        start,
        end,
        category_prefixes=category_prefixes,
        ungraded_only=ungraded_only,
        exclude_title_keywords=exclude_title_keywords,
    )


def list_coin_lots_on_day(tz: ZoneInfo, day: date) -> list[CoinTodayLot]:
    """指定业务日期截拍专场里的全部拍品（拍场页按日期筛选用）。"""
    start, end = day_bounds_local(tz, day)
    return _list_coin_lots_between(start, end)


def _list_coin_lots_between(
    start: str,
    end: str,
    *,
    category_prefixes: Sequence[str] | None = None,
    ungraded_only: bool = False,
    exclude_title_keywords: Sequence[str] | None = None,
) -> list[CoinTodayLot]:
    sql = """
        SELECT l.id, l.session_id, l.title, l.start_price, l.current_price,
               l.image_urls, s.title AS session_title, s.auction_at,
               sh.name AS shop_name, COALESCE(sh.followed, 0) AS shop_followed
        FROM coin_lots l
        JOIN coin_sessions s ON s.id = l.session_id
        LEFT JOIN coin_shops sh ON sh.id = s.shop_id
        WHERE s.auction_at >= ? AND s.auction_at < ?
    """
    params: list[object] = [start, end]
    if category_prefixes:
        sql += " AND (" + " OR ".join("l.category_code LIKE ?" for _ in category_prefixes) + ")"
        params.extend(f"{prefix}%" for prefix in category_prefixes)
    if ungraded_only:
        sql += " AND COALESCE(l.is_graded, 0) = 0"
    if exclude_title_keywords:
        sql += " AND " + " AND ".join("l.title NOT LIKE ?" for _ in exclude_title_keywords)
        params.extend(f"%{keyword}%" for keyword in exclude_title_keywords)
    sql += " ORDER BY s.auction_at, l.id"
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        CoinTodayLot(
            id=row["id"],
            session_id=row["session_id"],
            title=row["title"],
            start_price=row["start_price"],
            current_price=row["current_price"],
            image_urls=_json_list(row["image_urls"]),
            shop_name=row["shop_name"],
            session_title=row["session_title"],
            auction_at=row["auction_at"],
            shop_followed=bool(row["shop_followed"]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------- 好品推荐（铜币品相评级）


def insert_pending_coin_lot_grades(rows: Sequence[tuple[int, int]]) -> int:
    """匹配到的 `(lot_id, series_id)` 入队待评级；同一拍品只留一行（幂等）。"""
    if not rows:
        return 0
    now = now_local()
    with db.connect() as conn, conn:
        cursor = conn.executemany(
            """
            INSERT OR IGNORE INTO coin_lot_grades (lot_id, series_id, status, created_at)
            VALUES (?, ?, 'pending', ?)
            """,
            [(lot_id, series_id, now) for lot_id, series_id in rows],
        )
    return max(cursor.rowcount, 0)


def list_pending_grade_lots(limit: int) -> list[dict[str, object]]:
    """待评级队列（按入队顺序），附拍品标题与图片供评级调用。"""
    if limit <= 0:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT g.lot_id, g.series_id, l.title, l.image_urls
            FROM coin_lot_grades g
            JOIN coin_lots l ON l.id = g.lot_id
            WHERE g.status = 'pending'
            ORDER BY g.created_at, g.id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "lot_id": row["lot_id"],
            "series_id": row["series_id"],
            "title": row["title"],
            "image_urls": _json_list(row["image_urls"]),
        }
        for row in rows
    ]


def count_coin_graded_on_day(day: str | None = None) -> int:
    """某天（默认今天）已成功评级的条数，用于每日 50 条风控。"""
    target = day or now_local()[:10]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM coin_lot_grades "
            "WHERE status = 'graded' AND graded_at LIKE ?",
            (f"{target}%",),
        ).fetchone()
    return int(row["n"])


def mark_coin_lot_grade_failed(lot_id: int, error: str) -> None:
    with db.connect() as conn, conn:
        conn.execute(
            "UPDATE coin_lot_grades SET status = 'failed', error = ? WHERE lot_id = ?",
            ((error or "")[:500], lot_id),
        )


def save_coin_lot_grade(
    lot_id: int,
    parsed: dict[str, object],
    *,
    model_raw: str,
    model: str,
) -> None:
    with db.connect() as conn, conn:
        conn.execute(
            """
            UPDATE coin_lot_grades SET
                status = 'graded', grade_interval = ?, grade_score = ?, grade_cn = ?,
                color_grade = ?, wear_desc = ?, color_desc = ?, flaws = ?,
                blind_spots = ?, confidence = ?, net_grade = ?,
                model_raw = ?, model = ?, graded_at = ?, error = NULL
            WHERE lot_id = ?
            """,
            (
                parsed.get("grade_interval"),
                parsed.get("grade_score"),
                parsed.get("grade_cn"),
                parsed.get("color_grade"),
                parsed.get("wear_desc"),
                parsed.get("color_desc"),
                parsed.get("flaws"),
                parsed.get("blind_spots"),
                parsed.get("confidence"),
                parsed.get("net_grade"),
                model_raw,
                model,
                now_local(),
                lot_id,
            ),
        )


def get_coin_series_by_id(series_id: int, *, cn: bool = True) -> CoinSeries | None:
    """按内部主键取系列（`get_coin_series` 只按 numista_id 查）。"""
    table = "coin_series_cn" if cn else "coin_series"
    with db.connect() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (series_id,)).fetchone()
    return _row_to_coin_series(row) if row else None


def list_best_grade_lots(
    tz: ZoneInfo,
    *,
    min_score: int = 40,
    collection_key: str | None = None,
    now: datetime | None = None,
) -> list[CoinBestLot]:
    """今日 11070 类别、已评级且下界 >= `min_score` 的拍品，按分数降序。"""
    start, end = local_day_bounds(tz, now=now)
    sql = """
        SELECT l.id, l.session_id, l.title, l.start_price, l.current_price,
               l.image_urls, s.title AS session_title, s.auction_at,
               sh.name AS shop_name, COALESCE(sh.followed, 0) AS shop_followed,
               g.grade_interval, g.grade_score, g.color_grade,
               cs.issuer, cs.ruler, cs.denomination
        FROM coin_lot_grades g
        JOIN coin_lots l ON l.id = g.lot_id
        JOIN coin_sessions s ON s.id = l.session_id
        LEFT JOIN coin_shops sh ON sh.id = s.shop_id
        LEFT JOIN coin_series_cn cs ON cs.id = g.series_id
        WHERE g.status = 'graded' AND g.grade_score >= ?
          AND s.auction_at >= ? AND s.auction_at < ?
          AND l.category_code LIKE '11070%'
    """
    params: list[object] = [min_score, start, end]
    if collection_key:
        sql += """
          AND EXISTS (
              SELECT 1 FROM coin_collection_series c
              JOIN coin_collection_series_members m ON m.collection_id = c.id
              WHERE c.collection_key = ? AND m.numista_series_id = cs.numista_id
          )
        """
        params.append(collection_key)
    sql += " ORDER BY g.grade_score DESC, l.id"
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        CoinBestLot(
            id=row["id"],
            session_id=row["session_id"],
            title=row["title"],
            start_price=row["start_price"],
            current_price=row["current_price"],
            image_urls=_json_list(row["image_urls"]),
            shop_name=row["shop_name"],
            session_title=row["session_title"],
            auction_at=row["auction_at"],
            issuer=row["issuer"],
            ruler=row["ruler"],
            denomination=row["denomination"],
            grade_interval=row["grade_interval"],
            grade_score=row["grade_score"],
            color_grade=row["color_grade"],
            shop_followed=bool(row["shop_followed"]),
        )
        for row in rows
    ]


def search_coin_lots(
    keyword: str = "",
    *,
    price_min: float | None = None,
    price_max: float | None = None,
    auction_from: str | None = None,
    statuses: tuple[str, ...] = ("running", "preview"),
    limit: int = 300,
) -> list[CoinSearchLot]:
    """全局拍品搜索：FTS5 命中 + 价格区间 + 截拍日期下限 + 默认只取进行中 / 即将开始的拍场。"""
    match = textmatch.fts_query(keyword)
    clauses = [f"s.status IN ({', '.join('?' for _ in statuses)})"]
    params: list[object] = list(statuses)
    price = "COALESCE(l.current_price, l.start_price)"
    if price_min is not None:
        clauses.append(f"{price} >= ?")
        params.append(price_min)
    if price_max is not None:
        clauses.append(f"{price} <= ?")
        params.append(price_max)
    if auction_from is not None:
        clauses.append("s.auction_at >= ?")
        params.append(auction_from)
    if match:
        source = "coin_lot_fts JOIN coin_lots l ON l.id = coin_lot_fts.rowid"
        clauses.insert(0, "coin_lot_fts MATCH ?")
        params.insert(0, match)
        order = "rank"
    else:
        source = "coin_lots l"
        order = "s.auction_at, l.id"
    params.append(limit)
    sql = f"""
        SELECT l.id, l.session_id, l.lot_no, l.title, l.start_price, l.current_price,
               l.image_urls, l.tags, l.is_recommended,
               s.title AS session_title, s.status AS session_status,
               s.auction_at, s.auction_at_raw,
               sh.name AS shop_name, COALESCE(sh.followed, 0) AS shop_followed
        FROM {source}
        JOIN coin_sessions s ON s.id = l.session_id
        LEFT JOIN coin_shops sh ON sh.id = s.shop_id
        WHERE {' AND '.join(clauses)}
        ORDER BY {order}
        LIMIT ?
    """
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        CoinSearchLot(
            id=row["id"],
            session_id=row["session_id"],
            lot_no=row["lot_no"],
            title=row["title"],
            start_price=row["start_price"],
            current_price=row["current_price"],
            image_urls=_json_list(row["image_urls"]),
            tags=_json_dict(row["tags"]),
            session_title=row["session_title"],
            session_status=row["session_status"],
            auction_at=row["auction_at"],
            auction_at_raw=row["auction_at_raw"],
            shop_name=row["shop_name"],
            shop_followed=bool(row["shop_followed"]),
            is_recommended=bool(row["is_recommended"]),
        )
        for row in rows
    ]


def _row_to_coin_tag(row: sqlite3.Row) -> CoinProfileTag:
    return CoinProfileTag(
        id=row["id"],
        dimension=row["dimension"],
        value=row["value"],
        weight=row["weight"],
        hit_count=row["hit_count"],
        enabled=bool(row["enabled"]),
        note=row["note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_coin_profile_tags(only_enabled: bool = False) -> list[CoinProfileTag]:
    """画像标签，按权重降序（权重大 = 我更常买）。"""
    sql = "SELECT * FROM coin_profile_tags"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY weight DESC, id"
    with db.connect() as conn:
        return [_row_to_coin_tag(row) for row in conn.execute(sql)]


def upsert_coin_tag(dimension: str, value: str, weight: float, hit_count: int) -> None:
    """聚合结果写回画像标签。

    **保留人工编辑**：冲突时只更新 `weight` / `hit_count` / `updated_at`，
    不动 `enabled` / `note` —— 否则每次重跑 `coins-parse-orders` 都会把我关掉的标签打开
    （同 `sync_sources_from_config` 保留运行时状态的取舍）。
    """
    now = now_local()
    with db.connect() as conn, conn:
        conn.execute(
            """
            INSERT INTO coin_profile_tags
                (dimension, value, weight, hit_count, enabled, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, NULL, ?, ?)
            ON CONFLICT(dimension, value) DO UPDATE SET
                weight     = excluded.weight,
                hit_count  = excluded.hit_count,
                updated_at = excluded.updated_at
            """,
            (dimension, value, weight, hit_count, now, now),
        )


def add_coin_tag(dimension: str, value: str, weight: float = 1.0, note: str | None = None) -> bool:
    """手工新增标签；`(dimension, value)` 已存在则返回 False（不覆盖）。"""
    now = now_local()
    with db.connect() as conn, conn:
        cursor = conn.execute(
            """
            INSERT INTO coin_profile_tags
                (dimension, value, weight, hit_count, enabled, note, created_at, updated_at)
            VALUES (?, ?, ?, 0, 1, ?, ?, ?)
            ON CONFLICT(dimension, value) DO NOTHING
            """,
            (dimension, value, weight, note, now, now),
        )
        return cursor.rowcount > 0


def set_coin_tag_enabled(tag_id: int, enabled: bool) -> bool:
    with db.connect() as conn, conn:
        cursor = conn.execute(
            "UPDATE coin_profile_tags SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), now_local(), tag_id),
        )
        return cursor.rowcount > 0


def set_coin_tag_weight(tag_id: int, weight: float) -> bool:
    with db.connect() as conn, conn:
        cursor = conn.execute(
            "UPDATE coin_profile_tags SET weight = ?, updated_at = ? WHERE id = ?",
            (weight, now_local(), tag_id),
        )
        return cursor.rowcount > 0


def delete_coin_tag(tag_id: int) -> bool:
    with db.connect() as conn, conn:
        cursor = conn.execute("DELETE FROM coin_profile_tags WHERE id = ?", (tag_id,))
        return cursor.rowcount > 0


def get_coin_tag(tag_id: int) -> CoinProfileTag | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM coin_profile_tags WHERE id = ?", (tag_id,)).fetchone()
    return _row_to_coin_tag(row) if row else None


# ---------------------------------------------------------------- 店铺 / 专场 / 拍品


def upsert_coin_shop(shop: CoinShop) -> int:
    """按 `mxiqi_uid` upsert，返回 `coin_shops.id`。"""
    with db.connect() as conn, conn:
        conn.execute(
            """
            INSERT INTO coin_shops (mxiqi_uid, name, avatar_url, badges, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(mxiqi_uid) DO UPDATE SET
                name       = excluded.name,
                avatar_url = excluded.avatar_url,
                badges     = excluded.badges,
                fetched_at = excluded.fetched_at
            """,
            (shop.mxiqi_uid, shop.name, shop.avatar_url, _dump(shop.badges), shop.fetched_at),
        )
        row = conn.execute(
            "SELECT id FROM coin_shops WHERE mxiqi_uid = ?", (shop.mxiqi_uid,)
        ).fetchone()
        return int(row["id"])


def upsert_coin_session(session: CoinSession) -> int:
    """按 `mxiqi_id` upsert，返回 `coin_sessions.id`。"""
    with db.connect() as conn, conn:
        conn.execute(
            """
            INSERT INTO coin_sessions
                (mxiqi_id, shop_id, title, status, kind, auction_at, auction_at_raw,
                 deposit, lot_total, view_count, viewer_count, cover_urls, fetched_at,
                 commission_rate, commission_min, express_desc, express_day,
                 express_fee, express_free_amount, is_storage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mxiqi_id) DO UPDATE SET
                shop_id        = excluded.shop_id,
                title          = excluded.title,
                status         = excluded.status,
                kind           = excluded.kind,
                auction_at     = excluded.auction_at,
                auction_at_raw = excluded.auction_at_raw,
                deposit        = excluded.deposit,
                lot_total      = excluded.lot_total,
                view_count     = excluded.view_count,
                viewer_count   = excluded.viewer_count,
                cover_urls     = excluded.cover_urls,
                fetched_at     = excluded.fetched_at,
                commission_rate = excluded.commission_rate,
                commission_min  = excluded.commission_min,
                express_desc    = excluded.express_desc,
                express_day     = excluded.express_day,
                express_fee     = excluded.express_fee,
                express_free_amount = excluded.express_free_amount,
                is_storage      = excluded.is_storage
            """,
            (
                session.mxiqi_id,
                session.shop_id,
                session.title,
                session.status,
                session.kind,
                session.auction_at,
                session.auction_at_raw,
                session.deposit,
                session.lot_total,
                session.view_count,
                session.viewer_count,
                _dump(session.cover_urls),
                session.fetched_at,
                session.commission_rate,
                session.commission_min,
                session.express_desc,
                session.express_day,
                session.express_fee,
                session.express_free_amount,
                1 if session.is_storage else 0,
            ),
        )
        row = conn.execute(
            "SELECT id FROM coin_sessions WHERE mxiqi_id = ?", (session.mxiqi_id,)
        ).fetchone()
        return int(row["id"])


def set_coin_session_status(
    session_id: int,
    status: str,
    *,
    view_count: int | None = None,
    viewer_count: int | None = None,
) -> None:
    """同步拍场状态（回采终态时以站内三态为准：preview / running / closed）。

    `view_count` / `viewer_count` 分别是专场级累计围观次数（`auction_stat.pv`）
    与去重人数（`auction_user_total`）；未提供时保留旧值，不用 NULL 覆盖。
    """
    with db.connect() as conn, conn:
        conn.execute(
            "UPDATE coin_sessions SET status = ?, view_count = COALESCE(?, view_count),"
            " viewer_count = COALESCE(?, viewer_count) WHERE id = ?",
            (status, view_count, viewer_count, session_id),
        )


def upsert_coin_lot(lot: CoinLot) -> int:
    """按 `mxiqi_id` upsert，返回 `coin_lots.id`。

    匹配分与推荐位**只在首次入库时写**；后续重抓不覆盖已算过的匹配结果
    （匹配由 M9 的 matcher 单独更新，避免每次采集把推荐擦掉）。

    终态（`outcome` / `final_price` / `winner_uid` / `closed_at`）用 COALESCE 写：
    在拍场次重抓时这些字段是 NULL，不能把上一次回采到的终态擦掉。
    """
    with db.connect() as conn, conn:
        conn.execute(
            """
            INSERT INTO coin_lots
                (session_id, mxiqi_id, lot_no, title, start_price, current_price,
                 outcome, final_price, winner_uid, closed_at, category_code, is_graded,
                 image_urls, tags, match_score, is_recommended,
                 bid_num, bid_user_num, bid_source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mxiqi_id) DO UPDATE SET
                session_id    = excluded.session_id,
                lot_no        = excluded.lot_no,
                title         = excluded.title,
                start_price   = excluded.start_price,
                current_price = excluded.current_price,
                outcome       = COALESCE(excluded.outcome, coin_lots.outcome),
                final_price   = COALESCE(excluded.final_price, coin_lots.final_price),
                winner_uid    = COALESCE(excluded.winner_uid, coin_lots.winner_uid),
                closed_at     = COALESCE(excluded.closed_at, coin_lots.closed_at),
                -- 分类只在详情 / 回填带得到；缺键时不能擦掉已有值（plan §18.19.4）
                category_code = COALESCE(excluded.category_code, coin_lots.category_code),
                -- 评级判定只由标题得出，标题每次重抓都会覆盖，这里直接跟随
                is_graded     = excluded.is_graded,
                image_urls    = excluded.image_urls,
                tags          = excluded.tags,
                -- 热度只有预览 / 成交搜索带；auction.get 传 NULL，不能擦掉已有值
                bid_num       = COALESCE(excluded.bid_num, coin_lots.bid_num),
                bid_user_num  = COALESCE(excluded.bid_user_num, coin_lots.bid_user_num),
                bid_source    = COALESCE(excluded.bid_source, coin_lots.bid_source),
                fetched_at    = excluded.fetched_at
            """,
            (
                lot.session_id,
                lot.mxiqi_id,
                lot.lot_no,
                lot.title,
                lot.start_price,
                lot.current_price,
                lot.outcome,
                lot.final_price,
                lot.winner_uid,
                lot.closed_at,
                lot.category_code,
                lot.is_graded if lot.is_graded is not None else 0,
                _dump(lot.image_urls),
                _dump(lot.tags),
                lot.match_score,
                int(lot.is_recommended),
                lot.bid_num,
                lot.bid_user_num,
                lot.bid_source,
                lot.fetched_at,
            ),
        )
        row = conn.execute(
            "SELECT id FROM coin_lots WHERE mxiqi_id = ?", (lot.mxiqi_id,)
        ).fetchone()
        lot_id = int(row["id"])
        if lot.image_keys:
            # 同图识别只认 OSS key；每次按当前图集重建，避免删图后留下旧 key
            conn.execute("DELETE FROM coin_lot_images WHERE lot_id = ?", (lot_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO coin_lot_images (lot_id, oss_key) VALUES (?, ?)",
                [(lot_id, key) for key in lot.image_keys],
            )
        conn.execute("DELETE FROM coin_lot_fts WHERE rowid = ?", (lot_id,))
        conn.execute(
            "INSERT INTO coin_lot_fts(rowid, search_text) VALUES (?, ?)",
            (
                lot_id,
                textmatch.fts_text(
                    lot.title,
                    *lot.tags.keys(),
                    *lot.tags.values(),
                ),
            ),
        )
        return lot_id


def update_coin_lot_match(lot_id: int, match_score: float, is_recommended: bool) -> None:
    """写回匹配结果（matcher 用）。"""
    with db.connect() as conn, conn:
        conn.execute(
            "UPDATE coin_lots SET match_score = ?, is_recommended = ? WHERE id = ?",
            (match_score, int(is_recommended), lot_id),
        )


def update_coin_lot_matches(updates: list[tuple[int, float, bool]]) -> None:
    """批量写回匹配结果（页面打开前重算当日匹配用，一次事务）。"""
    if not updates:
        return
    with db.connect() as conn, conn:
        conn.executemany(
            "UPDATE coin_lots SET match_score = ?, is_recommended = ? WHERE id = ?",
            [(score, int(flag), lot_id) for lot_id, score, flag in updates],
        )


def update_coin_lot_bids(
    updates: list[tuple[str, int | None, int | None]], *, source: str = "home_filter"
) -> int:
    """按 `item_id`（`mxiqi_id` 冒号后的段）补热度，返回受影响行数。

    `home.filter` 拿不到专场 id，只能按拍品 id 后缀匹配；NULL 不覆盖已有值。
    """
    if not updates:
        return 0
    with db.connect() as conn, conn:
        total = 0
        for item_id, bid_num, bid_user_num in updates:
            cursor = conn.execute(
                """
                UPDATE coin_lots
                   SET bid_num      = COALESCE(?, bid_num),
                       bid_user_num = COALESCE(?, bid_user_num),
                       bid_source   = ?
                 WHERE mxiqi_id LIKE '%:' || ?
                """,
                (bid_num, bid_user_num, source, item_id),
            )
            total += cursor.rowcount
        return total


def update_coin_lot_filter_heat(
    rows: Sequence[dict[str, object]], *, source: str = "home_filter"
) -> tuple[int, int]:
    """按 `mxiqi_id` 精确回填 `home.filter` 的热度 / 中拍人 / 属性标签。

    `rows` 每项是 `filter_scan.update_row()` 的产物；NULL 不覆盖库里已有值
    （`bid_num` 缺键时保持原值，**不写 0**）。返回 `(更新行数, 未命中拍品数)` ——
    filter 是全站池，绝大多数条目不在我们的专场里，未命中是常态。
    """
    if not rows:
        return 0, 0
    updated = missing = 0
    with db.connect() as conn, conn:
        for row in rows:
            cursor = conn.execute(
                """
                UPDATE coin_lots
                   SET bid_num         = COALESCE(?, bid_num),
                       bid_user_num    = COALESCE(?, bid_user_num),
                       bid_users       = COALESCE(?, bid_users),
                       winner_uid      = COALESCE(?, winner_uid),
                       winner_nickname = COALESCE(?, winner_nickname),
                       tags            = COALESCE(?, tags),
                       bid_source      = ?
                 WHERE mxiqi_id = ?
                """,
                (
                    row.get("bid_num"),
                    row.get("bid_user_num"),
                    row.get("bid_users"),
                    row.get("winner_uid"),
                    row.get("winner_nickname"),
                    row.get("tags"),
                    source,
                    row["mxiqi_id"],
                ),
            )
            if cursor.rowcount:
                updated += cursor.rowcount
            else:
                missing += 1
    return updated, missing


# ---------------------------------------------------------------- 拍品分类（v21）


def upsert_coin_categories(
    rows: Sequence[
        tuple[str, str, int, str | None, int, str | None, int | None, int | None]
    ],
    *,
    prune: bool = False,
) -> None:
    """按 `code` upsert 分类字典的完整字段（v23；plan §18.19.4）。

    行序：`(code, name, sort, parent_code, level, thumb, item_count, order_no)`。
    `fetched_at` 保留首次写入时间，重复同步只更新 `updated_at`；
    `thumb` / `item_count` 缺省（种子兜底）时用 COALESCE 保住接口同步来的值。
    `prune=True` 删除接口里已消失的码；仍被拍品引用的保留，避免外键把历史数据置空。
    """
    if not rows:
        return
    now = now_local()
    with db.connect() as conn, conn:
        conn.executemany(
            """
            INSERT INTO coin_categories
                (code, name, sort, parent_code, level, thumb, item_count, order_no,
                 fetched_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name        = excluded.name,
                sort        = excluded.sort,
                parent_code = excluded.parent_code,
                level       = excluded.level,
                thumb       = COALESCE(excluded.thumb, coin_categories.thumb),
                item_count  = COALESCE(excluded.item_count, coin_categories.item_count),
                order_no    = excluded.order_no,
                updated_at  = excluded.updated_at
            """,
            [(*row, now, now) for row in rows],
        )
        if prune:
            codes = [row[0] for row in rows]
            placeholders = ",".join("?" * len(codes))
            conn.execute(
                f"""
                DELETE FROM coin_categories
                 WHERE code NOT IN ({placeholders})
                   AND NOT EXISTS (
                       SELECT 1 FROM coin_lots l WHERE l.category_code = coin_categories.code
                   )
                """,
                codes,
            )


def update_coin_lot_categories(updates: Sequence[tuple[str, str]]) -> int:
    """按 `mxiqi_id` 只写 `category_code`，返回受影响行数（历史回填用）。

    `updates` 是 `(mxiqi_id, category_code)`；COALESCE 保证站点缺分类时
    不擦掉库里已有的值（plan §18.19.5「只写 category_code，不动其他列」）。
    """
    if not updates:
        return 0
    with db.connect() as conn, conn:
        cursor = conn.executemany(
            """
            UPDATE coin_lots
               SET category_code = COALESCE(?, category_code)
             WHERE mxiqi_id = ?
            """,
            [(code, mxiqi_id) for mxiqi_id, code in updates],
        )
        return cursor.rowcount


def list_coin_sessions_pending_category(
    since: str, limit: int | None = None
) -> list[tuple[int, str]]:
    """待回填分类的专场：返回 `(session_id, mxiqi_id)`。

    按 `fetched_at` 升序（先补入库早的老场次）；`since` 是 `fetched_at` 下界；
    仍有 `category_code IS NULL` 拍品的场次才进来 —— 已全覆盖的场次直接跳过。
    `limit=None` 表示不限（SQLite 的 `LIMIT -1`）。
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.mxiqi_id
            FROM coin_sessions s
            WHERE s.fetched_at >= ?
              AND EXISTS (
                  SELECT 1 FROM coin_lots l
                  WHERE l.session_id = s.id AND l.category_code IS NULL
              )
            ORDER BY s.fetched_at ASC
            LIMIT ?
            """,
            (since, -1 if limit is None else limit),
        ).fetchall()
    return [(int(row["id"]), row["mxiqi_id"]) for row in rows]


def coin_lot_category_coverage() -> dict[str, int]:
    """拍品分类覆盖率：总数 / 有分类 / 无分类。"""
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS lots,
                   SUM(CASE WHEN category_code IS NOT NULL THEN 1 ELSE 0 END) AS categorized
              FROM coin_lots
            """
        ).fetchone()
    lots = int(row["lots"] or 0)
    categorized = int(row["categorized"] or 0)
    return {"lots": lots, "categorized": categorized, "uncategorized": lots - categorized}


def coin_category_fallback_codes() -> list[str]:
    """字典外的分类码（落库时以 `name = code` 兜底的那些），供回填后核对。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT code FROM coin_categories WHERE name = code ORDER BY code"
        ).fetchall()
    return [row["code"] for row in rows]


def existing_coin_category_codes(codes: Sequence[str]) -> set[str]:
    """给定的码里哪些已经在分类字典里（判断兜底是否真缺行，不覆盖接口同步来的名称）。"""
    if not codes:
        return set()
    placeholders = ",".join("?" * len(codes))
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT code FROM coin_categories WHERE code IN ({placeholders})",
            list(codes),
        ).fetchall()
    return {row["code"] for row in rows}


def list_coin_categories(level1: str | None = None) -> list[sqlite3.Row]:
    """分类字典（可按一级码过滤，如 `11000`），供分析页的下拉使用。"""
    sql = "SELECT code, name FROM coin_categories"
    params: list[str] = []
    if level1:
        sql += " WHERE parent_code = ?"
        params.append(level1)
    sql += " ORDER BY sort"
    with db.connect() as conn:
        return conn.execute(sql, params).fetchall()


def list_coin_lot_analysis_rows(
    *,
    level1: str = "110",
    category_code: str | None = None,
    only_ungraded: bool = True,
    days: int | None = None,
    shop_id: int | None = None,
) -> list[sqlite3.Row]:
    """分析页的拍品行（只读；plan §18.19.7）。

    - `level1`：一级分类前缀（硬币 `110`）；
    - `category_code`：切到某个二级分类；
    - `only_ungraded`：默认只看裸币，`COALESCE(is_graded, 0) = 0`；
    - `days`：按专场展示时间（缺失回落 `fetched_at`）取近 N 天；
    - `shop_id`：只看某店铺。
    """
    conditions = ["substr(l.category_code, 1, 3) = ?"]
    params: list[object] = [level1]
    if category_code:
        conditions.append("l.category_code = ?")
        params.append(category_code)
    if only_ungraded:
        conditions.append("COALESCE(l.is_graded, 0) = 0")
    if days is not None:
        since = to_local_str(datetime.now(timezone.utc) - timedelta(days=days))
        conditions.append("COALESCE(s.auction_at, l.fetched_at) >= ?")
        params.append(since)
    if shop_id is not None:
        conditions.append("s.shop_id = ?")
        params.append(shop_id)
    with db.connect() as conn:
        return conn.execute(
            f"""
            SELECT l.id, l.title, l.start_price, l.final_price, l.outcome,
                   l.category_code, COALESCE(c.name, l.category_code, '未分类') AS category_name,
                   l.is_graded, l.session_id, s.shop_id, s.auction_at, l.closed_at,
                   COALESCE(sh.name, '未知店铺') AS shop_name,
                   iv.value_f AS numista_value
              FROM coin_lots l
              JOIN coin_sessions s ON s.id = l.session_id
              LEFT JOIN coin_categories c ON c.code = l.category_code
              LEFT JOIN coin_shops sh ON sh.id = s.shop_id
              LEFT JOIN coin_issue_values iv ON iv.numista_id = l.issue_numista_id
             WHERE {' AND '.join(conditions)}
             ORDER BY l.id
            """,
            params,
        ).fetchall()


def list_coin_analysis_shops() -> list[sqlite3.Row]:
    """分析页的店铺下拉：只列「我们见过的场次」里的店铺（spec §14.19.4）。"""
    with db.connect() as conn:
        return conn.execute(
            """
            SELECT DISTINCT sh.id, sh.name
              FROM coin_shops sh
              JOIN coin_sessions s ON s.shop_id = sh.id
             ORDER BY sh.name
            """
        ).fetchall()


def list_coin_shop_analysis_rows(
    *,
    level1: str = "110",
    category_code: str | None = None,
    only_ungraded: bool = True,
    days: int | None = None,
) -> list[sqlite3.Row]:
    """店铺分析页的拍品级明细（一次取全量，聚合在 `app/coins/shops.py`）。

    与 `list_coin_lot_analysis_rows` 同口径（硬币 + 默认裸币 + 时间窗落
    `auction_at` / `fetched_at`），额外带出店铺、场次热度、交付字段与图片 key，
    让榜单 11 列指标一次算完，避免按店铺逐条查询。
    """
    conditions = ["substr(l.category_code, 1, 3) = ?"]
    params: list[object] = [level1]
    if category_code:
        conditions.append("l.category_code = ?")
        params.append(category_code)
    if only_ungraded:
        conditions.append("COALESCE(l.is_graded, 0) = 0")
    if days is not None:
        since = to_local_str(datetime.now(timezone.utc) - timedelta(days=days))
        conditions.append("COALESCE(s.auction_at, l.fetched_at) >= ?")
        params.append(since)
    with db.connect() as conn:
        return conn.execute(
            f"""
            SELECT l.id, l.title, l.start_price, l.final_price, l.outcome, l.closed_at,
                   l.category_code, l.bid_num, l.bid_user_num, l.bid_users,
                   l.winner_uid, l.session_id,
                   s.shop_id, s.title AS session_title, s.status AS session_status,
                   s.kind AS session_kind, s.auction_at, s.view_count, s.viewer_count,
                   s.commission_rate, s.commission_min, s.express_fee, s.express_day,
                   s.is_storage,
                   COALESCE(sh.name, '未知店铺') AS shop_name,
                   COALESCE(sh.followed, 0) AS shop_followed,
                   (SELECT GROUP_CONCAT(img.oss_key)
                      FROM coin_lot_images img WHERE img.lot_id = l.id) AS image_keys
              FROM coin_lots l
              JOIN coin_sessions s ON s.id = l.session_id
              LEFT JOIN coin_shops sh ON sh.id = s.shop_id
             WHERE {' AND '.join(conditions)}
             ORDER BY l.id
            """,
            params,
        ).fetchall()


def list_coin_shop_today_sessions(
    shop_ids: Sequence[int], tz: ZoneInfo, *, now: datetime | None = None
) -> dict[int, list[sqlite3.Row]]:
    """按店铺取「今天」的场次（`auction_at` 落在业务时区当日）。

    「今日」与拍场页同口径（`local_day_bounds` + `coin_sessions.auction_at`），
    供店铺分析页的「前往今日拍场」判断有没有可跳转的场次。
    """
    ids = [int(shop_id) for shop_id in shop_ids if shop_id is not None]
    if not ids:
        return {}
    start, end = local_day_bounds(tz, now=now)
    placeholders = ",".join("?" * len(ids))
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, shop_id, title, status, kind, auction_at
              FROM coin_sessions
             WHERE shop_id IN ({placeholders})
               AND auction_at IS NOT NULL
               AND auction_at >= ? AND auction_at < ?
             ORDER BY auction_at, id
            """,
            [*ids, start, end],
        ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row["shop_id"]), []).append(row)
    return grouped


def coin_lot_bid_coverage() -> list[sqlite3.Row]:
    """按来源统计热度覆盖率（`AVG` 自动忽略 NULL，不把缺值当 0）。"""
    with db.connect() as conn:
        return conn.execute(
            """
            SELECT COALESCE(bid_source, 'none') AS source,
                   COUNT(*) AS lots,
                   SUM(CASE WHEN bid_num IS NOT NULL THEN 1 ELSE 0 END) AS lots_with_bid,
                   SUM(CASE WHEN bid_user_num IS NOT NULL THEN 1 ELSE 0 END) AS lots_with_user,
                   ROUND(AVG(bid_num), 2) AS avg_bid_num
              FROM coin_lots
             GROUP BY COALESCE(bid_source, 'none')
             ORDER BY lots DESC
            """
        ).fetchall()


def coin_lot_image_duplicates(min_lots: int = 2, limit: int = 50) -> list[sqlite3.Row]:
    """同 OSS key 覆盖多个拍品的分组（同图识别，plan §18.18.1 P1-7）。"""
    with db.connect() as conn:
        return conn.execute(
            """
            SELECT oss_key, COUNT(DISTINCT lot_id) AS lot_count
              FROM coin_lot_images
             GROUP BY oss_key
            HAVING COUNT(DISTINCT lot_id) >= ?
             ORDER BY lot_count DESC, oss_key
             LIMIT ?
            """,
            (min_lots, limit),
        ).fetchall()


def coin_lot_image_duplicate_lot_count() -> int:
    """至少有一张图与其他拍品重复的拍品数。"""
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT lot_id) AS n
              FROM coin_lot_images
             WHERE oss_key IN (
                   SELECT oss_key FROM coin_lot_images
                    GROUP BY oss_key
                   HAVING COUNT(DISTINCT lot_id) > 1
             )
            """
        ).fetchone()
    return int(row["n"] or 0)


def list_coin_lots_for_matching() -> list[CoinLot]:
    """全部拍品（含未推荐），供 matcher 重算匹配分。"""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM coin_lots ORDER BY id").fetchall()
    return [_row_to_coin_lot(row) for row in rows]


def list_coin_lots_for_mapping() -> list[sqlite3.Row]:
    """拍品→币种映射的输入与现状（plan/coins.md §18.18.5）。"""
    with db.connect() as conn:
        return conn.execute(
            "SELECT id, title, tags, series_numista_id, issue_numista_id,"
            " match_method, confidence, match_candidates FROM coin_lots ORDER BY id"
        ).fetchall()


def update_coin_lot_mappings(
    updates: list[tuple[int | None, int | None, str, float, str | None, int]],
) -> None:
    """批量写回映射结果（末位是 `coin_lots.id`），一次事务。"""
    if not updates:
        return
    with db.connect() as conn, conn:
        conn.executemany(
            "UPDATE coin_lots SET series_numista_id = ?, issue_numista_id = ?,"
            " match_method = ?, confidence = ?, match_candidates = ? WHERE id = ?",
            updates,
        )


def list_coin_issue_years(*, cn: bool = False) -> list[sqlite3.Row]:
    """全部单版的 `(系列号, 单版号, 年份, 发行量, Frequency)`；年份取 `gregorian_year`，为空退回 `year`。"""
    series_table = "coin_series_cn" if cn else "coin_series"
    issue_table = "coin_issues_cn" if cn else "coin_issues"
    with db.connect() as conn:
        return conn.execute(
            f"SELECT s.numista_id AS series_numista_id, i.numista_id AS issue_numista_id,"
            f" COALESCE(i.gregorian_year, i.year) AS year,"
            f" i.mintage AS mintage, v.frequency AS frequency"
            f" FROM {issue_table} i JOIN {series_table} s ON s.id = i.series_id"
            f" LEFT JOIN coin_issue_values v ON v.numista_id = i.numista_id"
        ).fetchall()


def count_coin_lot_mapping_stats() -> dict[str, int]:
    """映射覆盖率：拍品总数 / 已映射系列 / 已映射单版。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(series_numista_id IS NOT NULL) AS series_mapped,"
            " SUM(issue_numista_id IS NOT NULL) AS issue_mapped FROM coin_lots"
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "series_mapped": int(row["series_mapped"] or 0),
        "issue_mapped": int(row["issue_mapped"] or 0),
    }


def count_coin_lot_match_methods() -> dict[str, int]:
    """按 `match_method` 统计拍品数（NULL 归到 `unmapped`）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(match_method, 'unmapped') AS method, COUNT(*) AS n"
            " FROM coin_lots GROUP BY method ORDER BY n DESC"
        ).fetchall()
    return {row["method"]: int(row["n"]) for row in rows}


def list_coin_sessions_pending_finalize(
    since: str, before: str, run_started: str, limit: int
) -> list[tuple[int, str]]:
    """待回采终态的专场：返回 `(session_id, mxiqi_id)`。

    `since` / `before` 是 UTC ISO8601 区间（`since` = 最近 N 天，`before` = 留出缓冲的当下）：
    实时专场存的是**开拍时间**，所以必须等整场结束（调用方给缓冲）。
    `run_started` 之前的采集时间才要补：本轮刚采过的场次数据是新的，不重复请求。
    没有拍品的专场不进来（避免每轮空抓）；已有终态的拍品不重复抓。
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.mxiqi_id
            FROM coin_sessions s
            WHERE s.auction_at IS NOT NULL
              AND s.auction_at >= ?
              AND s.auction_at <= ?
              AND s.fetched_at < ?
              AND EXISTS (
                  SELECT 1 FROM coin_lots l
                  WHERE l.session_id = s.id AND l.outcome IS NULL
              )
            ORDER BY s.auction_at DESC
            LIMIT ?
            """,
            (since, before, run_started, limit),
        ).fetchall()
    return [(int(row["id"]), row["mxiqi_id"]) for row in rows]


def _row_to_coin_lot(row: sqlite3.Row) -> CoinLot:
    return CoinLot(
        id=row["id"],
        session_id=row["session_id"],
        mxiqi_id=row["mxiqi_id"],
        lot_no=row["lot_no"],
        title=row["title"],
        start_price=row["start_price"],
        current_price=row["current_price"],
        image_urls=_json_list(row["image_urls"]),
        tags=_json_dict(row["tags"]),
        match_score=row["match_score"],
        is_recommended=bool(row["is_recommended"]),
        fetched_at=row["fetched_at"],
        outcome=row["outcome"],
        final_price=row["final_price"],
        winner_uid=row["winner_uid"],
        closed_at=row["closed_at"],
        bid_num=row["bid_num"],
        bid_user_num=row["bid_user_num"],
        bid_source=row["bid_source"],
        category_code=row["category_code"],
        is_graded=row["is_graded"],
    )


# ---------------------------------------------------------------- 推荐价取证


def upsert_coin_price_ref(ref: CoinPriceRef) -> None:
    """同一条拍品只留最新一条取证（重查覆盖）。"""
    with db.connect() as conn, conn:
        conn.execute(
            "DELETE FROM coin_price_refs WHERE lot_id = ?",
            (ref.lot_id,),
        )
        conn.execute(
            """
            INSERT INTO coin_price_refs
                (lot_id, keyword, sample_count, p25, median, p75, raw, queried_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref.lot_id,
                ref.keyword,
                ref.sample_count,
                ref.p25,
                ref.median,
                ref.p75,
                ref.raw,
                ref.queried_at,
            ),
        )


def get_coin_price_ref(lot_id: int) -> CoinPriceRef | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM coin_price_refs WHERE lot_id = ? ORDER BY id DESC LIMIT 1", (lot_id,)
        ).fetchone()
    if row is None:
        return None
    return CoinPriceRef(
        lot_id=row["lot_id"],
        keyword=row["keyword"],
        sample_count=row["sample_count"],
        p25=row["p25"],
        median=row["median"],
        p75=row["p75"],
        raw=row["raw"],
        queried_at=row["queried_at"],
    )


# ---------------------------------------------------------------- 成交明细


def upsert_coin_sales(sales: list[CoinSale]) -> int:
    """按 `item_id` 写成交明细，返回提交条数（不是新增行数）。

    来源优先级：`session_final`（已结束专场的终态）高于 `sold_search`（站内成交搜索）；
    已有高置信行时，中置信数据不能覆盖它。
    """
    if not sales:
        return 0
    with db.connect() as conn, conn:
        conn.executemany(
            """
            INSERT INTO coin_sales
                (item_id, title, price, sold_at, session_id, shop_id, keyword, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                title      = excluded.title,
                price      = excluded.price,
                sold_at    = COALESCE(excluded.sold_at, coin_sales.sold_at),
                session_id = COALESCE(excluded.session_id, coin_sales.session_id),
                shop_id    = COALESCE(excluded.shop_id, coin_sales.shop_id),
                keyword    = COALESCE(excluded.keyword, coin_sales.keyword),
                source     = CASE
                                 WHEN coin_sales.source = 'session_final' THEN 'session_final'
                                 ELSE excluded.source
                             END,
                fetched_at = excluded.fetched_at
            WHERE coin_sales.source <> 'session_final' OR excluded.source = 'session_final'
            """,
            [
                (
                    sale.item_id,
                    sale.title,
                    sale.price,
                    sale.sold_at,
                    sale.session_id,
                    sale.shop_id,
                    sale.keyword,
                    sale.source,
                    sale.fetched_at,
                )
                for sale in sales
            ],
        )
    return len(sales)


def list_coin_sold_lots() -> list[CoinSale]:
    """把已回采终态（`outcome='sold'`）的拍品转成成交行，`source=session_final`。

    未落库，由调用方交给 `upsert_coin_sales`；`item_id` 从「专场 id:站内拍品 id」里取后段。
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT l.mxiqi_id, l.title, l.final_price, l.closed_at, l.session_id, s.shop_id
            FROM coin_lots l
            JOIN coin_sessions s ON s.id = l.session_id
            WHERE l.outcome = 'sold' AND l.final_price IS NOT NULL
            """
        ).fetchall()
    now = now_local()
    return [
        CoinSale(
            id=None,
            item_id=str(row["mxiqi_id"]).split(":", 1)[-1],
            title=row["title"],
            price=row["final_price"],
            sold_at=row["closed_at"],
            session_id=row["session_id"],
            shop_id=row["shop_id"],
            keyword=None,
            source="session_final",
            fetched_at=now,
        )
        for row in rows
    ]


def list_coin_sales(source: str | None = None) -> list[CoinSale]:
    """成交明细（可按来源过滤），按成交时间倒序。"""
    sql = "SELECT * FROM coin_sales"
    params: tuple = ()
    if source:
        sql += " WHERE source = ?"
        params = (source,)
    sql += " ORDER BY sold_at DESC, id DESC"
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        CoinSale(
            id=row["id"],
            item_id=row["item_id"],
            title=row["title"],
            price=row["price"],
            sold_at=row["sold_at"],
            session_id=row["session_id"],
            shop_id=row["shop_id"],
            keyword=row["keyword"],
            source=row["source"],
            fetched_at=row["fetched_at"],
        )
        for row in rows
    ]


def list_coin_series_sales(numista_id: int) -> list[sqlite3.Row]:
    """某系列下已映射的麦稀奇成交（只有 `session_final` 能映射到系列）。

    成交（`coin_sales.item_id`）与拍品（`coin_lots.mxiqi_id`）一一对应，
    系列 / 单版映射、标题、图片都在 `coin_lots` 上：拍品名称取 `coin_lots.title`，
    专场名称取 `coin_sessions.title`。`currency` 是该单版的估值币种，供中位数按
    「币种 + 单版」分组时使用（没有估值行时由调用方回落系列币种）。
    """
    sql = """
        SELECT s.item_id, s.price, s.sold_at,
               l.id AS lot_id, l.title AS lot_title, l.image_urls, l.tags,
               l.issue_numista_id,
               se.title AS session_title,
               sh.name AS shop_name,
               iv.currency AS currency
          FROM coin_sales s
          JOIN coin_lots l
            ON substr(l.mxiqi_id, instr(l.mxiqi_id, ':') + 1) = s.item_id
          JOIN coin_sessions se ON se.id = l.session_id
          LEFT JOIN coin_shops sh ON sh.id = se.shop_id
          LEFT JOIN coin_issue_values iv ON iv.numista_id = l.issue_numista_id
         WHERE s.source = 'session_final' AND l.series_numista_id = ?
         ORDER BY s.sold_at DESC, s.id DESC
    """
    with db.connect() as conn:
        return conn.execute(sql, (numista_id,)).fetchall()


def count_coin_sales_by_source() -> dict[str, int]:
    """按来源统计成交行数（验收 / 日志用）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM coin_sales GROUP BY source"
        ).fetchall()
    return {row["source"]: int(row["n"]) for row in rows}


# ---------------------------------------------------------------- 采集日志


def record_coin_fetch_run(run: CoinFetchRun, *, conn: sqlite3.Connection | None = None) -> int:
    with _write_conn(conn) as active:
        cursor = active.execute(
            """
            INSERT INTO coin_fetch_runs
                (kind, target, started_at, finished_at, status, http_status, items_new, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.kind,
                run.target,
                run.started_at,
                run.finished_at,
                run.status,
                run.http_status,
                run.items_new,
                run.error,
            ),
        )
        return int(cursor.lastrowid)


def latest_coin_fetch_runs(limit: int = 20) -> list[CoinFetchRun]:
    """最近若干条采集日志（任何 kind），供页面显示「数据停在什么时候」。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM coin_fetch_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        CoinFetchRun(
            id=row["id"],
            kind=row["kind"],
            target=row["target"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            http_status=row["http_status"],
            items_new=row["items_new"],
            error=row["error"],
        )
        for row in rows
    ]


def coin_fetch_summary() -> dict[str, object]:
    """给页面顶部用的汇总：最近一次成功时间 + 最近一条日志的状态。

    2026-10-12 起采集/查价都走免登录的小程序网关，不再有「反爬拦下 / 需要登录」两种状态。
    """
    with db.connect() as conn:
        last_ok = conn.execute(
            "SELECT MAX(finished_at) AS ts FROM coin_fetch_runs WHERE status = 'ok'"
        ).fetchone()["ts"]
        last = conn.execute("SELECT * FROM coin_fetch_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "last_ok_at": last_ok,
        "last_status": last["status"] if last else None,
        "last_error": last["error"] if last else None,
    }


# ---------------------------------------------------------------- 页面查询


_SESSION_CARD_SQL = """
    SELECT s.id AS session_id, s.mxiqi_id, s.title, s.status, s.kind,
           s.auction_at, s.auction_at_raw, s.lot_total, s.view_count, s.viewer_count,
           s.cover_urls,
           sh.name AS shop_name, sh.avatar_url AS shop_avatar,
           COALESCE(sh.followed, 0) AS shop_followed,
           (SELECT COUNT(*) FROM coin_lots l
             WHERE l.session_id = s.id AND l.is_recommended = 1) AS recommended_count
    FROM coin_sessions s
    LEFT JOIN coin_shops sh ON sh.id = s.shop_id
"""


def _row_to_session_card(row: sqlite3.Row) -> CoinSessionCard:
    return CoinSessionCard(
        session_id=row["session_id"],
        mxiqi_id=row["mxiqi_id"],
        title=row["title"],
        status=row["status"],
        auction_at=row["auction_at"],
        auction_at_raw=row["auction_at_raw"],
        lot_total=row["lot_total"],
        view_count=row["view_count"],
        cover_urls=_json_list(row["cover_urls"]),
        shop_name=row["shop_name"],
        shop_avatar=row["shop_avatar"],
        recommended_count=row["recommended_count"],
        shop_followed=bool(row["shop_followed"]),
        kind=row["kind"],
        viewer_count=row["viewer_count"],
    )


def list_coin_session_cards(
    limit: int | None = None,
    *,
    day_bounds: tuple[str, str] | None = None,
    only_recommended: bool = True,
    kind: str | None = None,
) -> list[CoinSessionCard]:
    """第一页：默认只列出至少命中 1 条推荐拍品的专场（spec §14.7「没有就不展示」）。

    `only_recommended=False` 时返回当日全部专场，供拍场页「是否推荐=全部」使用。
    未命中的专场仍全量入库，只是不默认出现 —— 画像改动后可以重新匹配，不必重抓。
    `day_bounds` 给定时只取展示时间落在该 `[start, end)` 区间（UTC 存储格式）的专场；
    `kind` 给定时只取对应类型（live=实时 / timed=限时）。
    """
    conditions: list[str] = []
    if only_recommended:
        conditions.append(
            """(SELECT COUNT(*) FROM coin_lots l
                WHERE l.session_id = s.id AND l.is_recommended = 1) > 0"""
        )
    params: list[object] = []
    if day_bounds is not None:
        conditions.append("s.auction_at >= ? AND s.auction_at < ?")
        params.extend(day_bounds)
    if kind is not None:
        conditions.append("s.kind = ?")
        params.append(kind)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = (
        _SESSION_CARD_SQL
        + where
        + """
    ORDER BY (s.auction_at IS NULL), s.auction_at DESC, s.id DESC
    """
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_session_card(row) for row in rows]


def get_coin_session_card(session_id: int) -> CoinSessionCard | None:
    with db.connect() as conn:
        row = conn.execute(
            _SESSION_CARD_SQL + " WHERE s.id = ?", (session_id,)
        ).fetchone()
    return _row_to_session_card(row) if row else None


def _row_to_coin_shop(row: sqlite3.Row) -> CoinShop:
    return CoinShop(
        id=row["id"],
        mxiqi_uid=row["mxiqi_uid"],
        name=row["name"],
        avatar_url=row["avatar_url"],
        badges=_json_list_badges(row["badges"]),
        fetched_at=row["fetched_at"],
        followed=bool(row["followed"]),
    )


def set_coin_shop_followed(shop_id: int, followed: bool) -> None:
    """关注 / 取消关注店铺（只改本地标记，不影响采集 upsert）。"""
    with db.connect() as conn, conn:
        conn.execute(
            "UPDATE coin_shops SET followed = ? WHERE id = ?",
            (1 if followed else 0, shop_id),
        )


def _json_list_badges(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def get_coin_session_detail(session_id: int) -> CoinSessionDetail | None:
    """第二页：店铺头 + 推荐拍品（按匹配分降序），每条拍品带上推荐价取证。"""
    with db.connect() as conn:
        session = conn.execute(
            "SELECT * FROM coin_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            return None
        shop_row = (
            conn.execute("SELECT * FROM coin_shops WHERE id = ?", (session["shop_id"],)).fetchone()
            if session["shop_id"] is not None
            else None
        )
        lot_rows = conn.execute(
            """
            SELECT * FROM coin_lots
            WHERE session_id = ?
            ORDER BY is_recommended DESC, match_score DESC, (lot_no IS NULL),
                     CAST(REPLACE(lot_no, 'Lot.', '') AS INTEGER), lot_no, id
            """,
            (session_id,),
        ).fetchall()
        ref_rows = conn.execute(
            """
            SELECT * FROM coin_price_refs
            WHERE lot_id IN (SELECT id FROM coin_lots WHERE session_id = ?)
            """,
            (session_id,),
        ).fetchall()

    refs = {row["lot_id"]: row for row in ref_rows}
    lots = [_row_to_lot_card(row, refs.get(row["id"])) for row in lot_rows]
    return CoinSessionDetail(
        session_id=session["id"],
        mxiqi_id=session["mxiqi_id"],
        title=session["title"],
        status=session["status"],
        kind=session["kind"],
        auction_at=session["auction_at"],
        auction_at_raw=session["auction_at_raw"],
        deposit=session["deposit"],
        lot_total=session["lot_total"],
        view_count=session["view_count"],
        viewer_count=session["viewer_count"],
        recommended_count=sum(1 for lot in lots if lot.is_recommended),
        shop=_row_to_coin_shop(shop_row) if shop_row else None,
        lots=lots,
    )


def _row_to_lot_card(row: sqlite3.Row, ref: sqlite3.Row | None) -> CoinLotCard:
    return CoinLotCard(
        lot_id=row["id"],
        lot_no=row["lot_no"],
        title=row["title"],
        start_price=row["start_price"],
        current_price=row["current_price"],
        image_urls=_json_list(row["image_urls"]),
        tags=_json_dict(row["tags"]),
        match_score=row["match_score"],
        keyword=ref["keyword"] if ref else None,
        sample_count=ref["sample_count"] if ref else None,
        p25=ref["p25"] if ref else None,
        median=ref["median"] if ref else None,
        p75=ref["p75"] if ref else None,
        is_recommended=bool(row["is_recommended"]),
        outcome=row["outcome"],
        final_price=row["final_price"],
    )


def count_coin_sessions() -> int:
    with db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM coin_sessions").fetchone()["n"])


def count_coin_lots() -> int:
    with db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM coin_lots").fetchone()["n"])


def count_recommended_lots() -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM coin_lots WHERE is_recommended = 1"
        ).fetchone()
        return int(row["n"])


# ---------------------------------------------------------------- Numista 参考库（系列 / 单版）
#
# 英文表与中文表（*_cn）列结构**完全一致**，只有可翻译字段的内容不同 —— 故共用同一段 upsert。

_ALLOWED_TABLES = frozenset(
    ("coin_series", "coin_series_cn", "coin_issues", "coin_issues_cn", "coin_issue_values")
)

_SERIES_COLUMNS = (
    "numista_id", "title", "url", "object_type", "issuer", "min_year", "max_year", "ruler",
    "denomination", "size_mm", "weight_g", "thickness_mm", "shape", "composition", "edge",
    "obverse_desc", "obverse_lettering", "obverse_picture",
    "reverse_desc", "reverse_lettering", "reverse_picture", "catalog_no", "comments", "mints",
    "series", "commemorated_topic", "fetched_at",
)
_ISSUE_COLUMNS = (
    "numista_id", "series_id", "year", "gregorian_year", "mint", "mint_mark", "variety", "comment",
    "mintage", "size_mm", "weight_g", "thickness_mm", "edge", "is_proof", "is_essai", "picture",
    "catalog_no", "fetched_at",
)
# 估值表不与 coin_issues 共主键：它按**单版编号**对齐（页面可能与接口不同批采到）
_VALUES_COLUMNS = (
    "numista_id", "series_numista_id", "currency",
    "value_g", "value_vg", "value_f", "value_vf", "value_xf", "value_au", "value_unc",
    "frequency", "fetched_at",
)


def _upsert_numista_row(table: str, columns: tuple[str, ...], values: tuple) -> int:
    """按 `numista_id` upsert（`values[0]` 即 numista_id），返回内部主键 id。"""
    if table not in _ALLOWED_TABLES:  # pragma: no cover - 只有误用才走到
        raise ValueError(f"未知表 {table!r}")
    placeholders = ", ".join("?" * len(columns))
    updates = ", ".join(f"{col} = excluded.{col}" for col in columns if col != "numista_id")
    with db.connect() as conn, conn:
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(numista_id) DO UPDATE SET {updates}",
            values,
        )
        row = conn.execute(f"SELECT id FROM {table} WHERE numista_id = ?", (values[0],)).fetchone()
        return int(row["id"])


def _series_values(series: CoinSeries) -> tuple:
    return (
        series.numista_id,
        series.title,
        series.url,
        series.object_type,
        series.issuer,
        series.min_year,
        series.max_year,
        series.ruler,
        series.denomination,
        series.size_mm,
        series.weight_g,
        series.thickness_mm,
        series.shape,
        series.composition,
        series.edge,
        series.obverse_desc,
        series.obverse_lettering,
        series.obverse_picture,
        series.reverse_desc,
        series.reverse_lettering,
        series.reverse_picture,
        series.catalog_no,
        series.comments,
        _dump(series.mints),
        _dump(series.series),
        series.commemorated_topic,
        series.fetched_at,
    )


def _issue_values(issue: CoinIssue) -> tuple:
    return (
        issue.numista_id,
        issue.series_id,
        issue.year,
        issue.gregorian_year,
        issue.mint,
        issue.mint_mark,
        issue.variety,
        issue.comment,
        issue.mintage,
        issue.size_mm,
        issue.weight_g,
        issue.thickness_mm,
        issue.edge,
        None if issue.is_proof is None else int(issue.is_proof),
        None if issue.is_essai is None else int(issue.is_essai),
        issue.picture,
        issue.catalog_no,
        issue.fetched_at,
    )


def upsert_coin_series(series: CoinSeries) -> int:
    """按 `numista_id` upsert（英文表 `coin_series`），返回内部 id。"""
    return _upsert_numista_row("coin_series", _SERIES_COLUMNS, _series_values(series))


def upsert_coin_series_cn(series: CoinSeries) -> int:
    """按 `numista_id` upsert（中文表 `coin_series_cn`）。字段一致，内容已翻译。"""
    return _upsert_numista_row("coin_series_cn", _SERIES_COLUMNS, _series_values(series))


def upsert_coin_issue(issue: CoinIssue) -> int:
    """按 `numista_id` upsert（英文表 `coin_issues`），返回内部 id。"""
    return _upsert_numista_row("coin_issues", _ISSUE_COLUMNS, _issue_values(issue))


def upsert_coin_issue_cn(issue: CoinIssue) -> int:
    """按 `numista_id` upsert（中文表 `coin_issues_cn`）。字段一致，内容已翻译。"""
    return _upsert_numista_row("coin_issues_cn", _ISSUE_COLUMNS, _issue_values(issue))


def _row_to_coin_series(row: sqlite3.Row) -> CoinSeries:
    return CoinSeries(
        id=row["id"],
        numista_id=row["numista_id"],
        title=row["title"],
        url=row["url"],
        object_type=row["object_type"],
        issuer=row["issuer"],
        min_year=row["min_year"],
        max_year=row["max_year"],
        ruler=row["ruler"],
        denomination=row["denomination"],
        size_mm=row["size_mm"],
        weight_g=row["weight_g"],
        thickness_mm=row["thickness_mm"],
        shape=row["shape"],
        composition=row["composition"],
        edge=row["edge"],
        obverse_desc=row["obverse_desc"],
        obverse_lettering=row["obverse_lettering"],
        obverse_picture=row["obverse_picture"],
        reverse_desc=row["reverse_desc"],
        reverse_lettering=row["reverse_lettering"],
        reverse_picture=row["reverse_picture"],
        catalog_no=row["catalog_no"],
        comments=row["comments"],
        mints=_json_list(row["mints"]),
        fetched_at=row["fetched_at"],
        series=_json_list(row["series"]),
        commemorated_topic=row["commemorated_topic"],
        issue_series=row["issue_series"] or "",
        issue_theme=row["issue_theme"] or "",
    )


def _row_to_coin_issue(row: sqlite3.Row) -> CoinIssue:
    is_proof = row["is_proof"]
    is_essai = row["is_essai"]
    return CoinIssue(
        id=row["id"],
        numista_id=row["numista_id"],
        series_id=row["series_id"],
        year=row["year"],
        gregorian_year=row["gregorian_year"],
        mint=row["mint"],
        mint_mark=row["mint_mark"],
        variety=row["variety"],
        comment=grades.fix_roc_comment(
            translate.fix_reversed_roc_legend(row["comment"]), row["year"]
        ),
        mintage=row["mintage"],
        size_mm=row["size_mm"],
        weight_g=row["weight_g"],
        thickness_mm=row["thickness_mm"],
        edge=row["edge"],
        is_proof=None if is_proof is None else bool(is_proof),
        is_essai=None if is_essai is None else bool(is_essai),
        picture=row["picture"],
        catalog_no=row["catalog_no"],
        fetched_at=row["fetched_at"],
    )


def get_coin_series(numista_id: int, *, cn: bool = False) -> CoinSeries | None:
    table = "coin_series_cn" if cn else "coin_series"
    with db.connect() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE numista_id = ?", (numista_id,)).fetchone()
    return _row_to_coin_series(row) if row else None


def list_coin_series(*, cn: bool = False) -> list[CoinSeries]:
    table = "coin_series_cn" if cn else "coin_series"
    with db.connect() as conn:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY numista_id").fetchall()
    return [_row_to_coin_series(row) for row in rows]


def list_coin_issues(series_id: int, *, cn: bool = False) -> list[CoinIssue]:
    """某系列的全部单版（按年份、编号排序）。`series_id` 是**对应表**的内部主键（`coin_series(.cn).id`）。"""
    table = "coin_issues_cn" if cn else "coin_issues"
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE series_id = ? ORDER BY year, id", (series_id,)
        ).fetchall()
    return [_row_to_coin_issue(row) for row in rows]


def count_coin_series(*, cn: bool = False) -> int:
    table = "coin_series_cn" if cn else "coin_series"
    with db.connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def count_coin_issues(*, cn: bool = False) -> int:
    table = "coin_issues_cn" if cn else "coin_issues"
    with db.connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def delete_coin_series(numista_id: int) -> bool:
    """按 Numista 系列号删除系列及其单版 / 估值（中英两张表）。

    `coin_issues[_cn]` 靠外键 `ON DELETE CASCADE` 连带删除（连接已开 `PRAGMA foreign_keys=ON`）；
    `coin_issue_values` 不设外键（按 numista_id 对齐），显式删。返回是否命中过该系列。
    """
    with db.connect() as conn:
        with conn:
            conn.execute(
                "DELETE FROM coin_issue_values WHERE series_numista_id = ?", (numista_id,)
            )
            deleted = conn.execute(
                "DELETE FROM coin_series WHERE numista_id = ?", (numista_id,)
            ).rowcount
            conn.execute("DELETE FROM coin_series_cn WHERE numista_id = ?", (numista_id,))
    return deleted > 0


def list_coin_series_countries(*, cn: bool = False) -> list[str]:
    """已采集系列的国家（`issuer`）去重清单，供查询页的国家下拉。"""
    table = "coin_series_cn" if cn else "coin_series"
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT issuer FROM {table} "
            "WHERE issuer IS NOT NULL AND issuer <> '' ORDER BY issuer"
        ).fetchall()
    return [row["issuer"] for row in rows]


def list_coin_series_denominations(*, cn: bool = False) -> list[str]:
    """已采集系列的面值去重清单，供查询页的面值下拉（可输入）。"""
    table = "coin_series_cn" if cn else "coin_series"
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT denomination FROM {table} "
            "WHERE denomination IS NOT NULL AND denomination <> '' ORDER BY denomination"
        ).fetchall()
    return [row["denomination"] for row in rows]


def list_coin_series_compositions(*, cn: bool = False) -> list[str]:
    """已采集系列的材质去重清单，供查询页的材质下拉（可输入）。"""
    table = "coin_series_cn" if cn else "coin_series"
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT composition FROM {table} "
            "WHERE composition IS NOT NULL AND composition <> '' ORDER BY composition"
        ).fetchall()
    return [row["composition"] for row in rows]


def list_coin_series_object_types(*, cn: bool = False) -> list[str]:
    """已采集系列的货币类型去重清单，供币库查询页的下拉。"""
    table = "coin_series_cn" if cn else "coin_series"
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT object_type FROM {table} "
            "WHERE object_type IS NOT NULL AND object_type <> '' ORDER BY object_type"
        ).fetchall()
    return [row["object_type"] for row in rows]


def search_coin_series(
    *,
    issuer: str | None = None,
    year: int | None = None,
    denomination: str | None = None,
    composition: str | None = None,
    object_type: str | None = None,
    size_min: float | None = None,
    size_max: float | None = None,
    numista_ids: Sequence[int] | None = None,
    cn: bool = False,
) -> list[CoinSeries]:
    """按国家 / 年份 / 面值 / 材质 / 货币类型 / 尺寸范围查已采集的系列（年份落在 `[min_year, max_year]` 即命中）。

    参数均可选：全空时返回全部系列。国家 / 货币类型走参数化 SQL（精确匹配）；
    面值 / 材质在取回后做归一化宽松匹配（要处理库值里的空格与数字边界，见 `_text_hit`）。
    尺寸给 `size_min` / `size_max` 时按 `size_mm` 闭区间筛（**无尺寸的系列不命中**）。
    `numista_ids` 给定时只在这些系列里查（观察系列页用；空列表直接返回空）。
    """
    table = "coin_series_cn" if cn else "coin_series"
    clauses: list[str] = []
    params: list[object] = []
    if issuer:
        clauses.append("issuer = ?")
        params.append(issuer)
    if object_type:
        clauses.append("object_type = ?")
        params.append(object_type)
    if numista_ids is not None:
        ids = list(numista_ids)
        if not ids:
            return []
        clauses.append(f"numista_id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if year is not None:
        clauses.append("(min_year IS NULL OR min_year <= ?) AND (max_year IS NULL OR max_year >= ?)")
        params.extend([year, year])
    if size_min is not None:
        clauses.append("size_mm >= ?")
        params.append(size_min)
    if size_max is not None:
        clauses.append("size_mm <= ?")
        params.append(size_max)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {table}{where} ORDER BY issuer, min_year, numista_id", params
        ).fetchall()
    series_list = [_row_to_coin_series(row) for row in rows]
    if denomination:
        series_list = [s for s in series_list if textmatch.contains(s.denomination, denomination)]
    if composition:
        series_list = [s for s in series_list if textmatch.contains(s.composition, composition)]
    return series_list


def coin_series_issue_stats(*, cn: bool = False) -> dict[int, CoinSeriesIssueStats]:
    """`{系列 numista_id: (不同年份数, 版别数)}`，供列表显示「N 个年份 · M 个版别」。

    年份数用 `COUNT(DISTINCT i.year)`（一个年份可能对应多个版别），版别数即单版条数；
    无单版的系列两块都是 0。一次查询，不逐系列再查。
    """
    series_table = "coin_series_cn" if cn else "coin_series"
    issue_table = "coin_issues_cn" if cn else "coin_issues"
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT s.numista_id AS numista_id, "
            "COUNT(DISTINCT i.year) AS years, COUNT(i.id) AS n "
            f"FROM {series_table} s LEFT JOIN {issue_table} i ON i.series_id = s.id "
            "GROUP BY s.numista_id"
        ).fetchall()
    return {
        int(row["numista_id"]): CoinSeriesIssueStats(
            year_count=int(row["years"]), issue_count=int(row["n"])
        )
        for row in rows
    }


def _values_values(values: CoinIssueValues) -> tuple:
    return (
        values.numista_id,
        values.series_numista_id,
        values.currency,
        values.value_g,
        values.value_vg,
        values.value_f,
        values.value_vf,
        values.value_xf,
        values.value_au,
        values.value_unc,
        values.frequency,
        values.fetched_at,
    )


def upsert_coin_issue_values(values: CoinIssueValues) -> int:
    """按 `numista_id`（单版编号）upsert 估值 + Frequency，返回内部 id。"""
    return _upsert_numista_row("coin_issue_values", _VALUES_COLUMNS, _values_values(values))


def _row_to_coin_issue_values(row: sqlite3.Row) -> CoinIssueValues:
    return CoinIssueValues(
        id=row["id"],
        numista_id=row["numista_id"],
        series_numista_id=row["series_numista_id"],
        currency=row["currency"],
        value_g=row["value_g"],
        value_vg=row["value_vg"],
        value_f=row["value_f"],
        value_vf=row["value_vf"],
        value_xf=row["value_xf"],
        value_au=row["value_au"],
        value_unc=row["value_unc"],
        frequency=row["frequency"],
        fetched_at=row["fetched_at"],
    )


def get_coin_issue_values(numista_id: int) -> CoinIssueValues | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM coin_issue_values WHERE numista_id = ?", (numista_id,)
        ).fetchone()
    return _row_to_coin_issue_values(row) if row else None


def list_coin_issue_values(series_numista_id: int) -> list[CoinIssueValues]:
    """某系列号下的全部单版估值（按单版编号排序）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM coin_issue_values WHERE series_numista_id = ? ORDER BY numista_id",
            (series_numista_id,),
        ).fetchall()
    return [_row_to_coin_issue_values(row) for row in rows]


def count_coin_issue_values() -> int:
    with db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM coin_issue_values").fetchone()["n"])


def coin_series_value_counts() -> dict[int, int]:
    """`{系列 numista_id: 已采集估值条数}`，供管理页显示该系列「估值 已采 / 未采」。

    按 `series_numista_id` 聚合，一次查询（不逐系列再查）。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT series_numista_id AS sid, COUNT(*) AS n "
            "FROM coin_issue_values GROUP BY series_numista_id"
        ).fetchall()
    return {int(row["sid"]): int(row["n"]) for row in rows}


# ---------------------------------------------------------------- Numista 调用计数（风控）


def record_api_call(provider: str) -> None:
    """记一次外部接口调用（用于月度额度自管）。"""
    with db.connect() as conn, conn:
        conn.execute(
            "INSERT INTO coin_api_usage (provider, called_at) VALUES (?, ?)",
            (provider, now_local()),
        )


def count_api_calls_since(provider: str, since_utc: str) -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM coin_api_usage WHERE provider = ? AND called_at >= ?",
            (provider, since_utc),
        ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------- home.filter 断点续扫


def coin_filter_scan_pages() -> dict[str, int]:
    """各二级分类下一轮从第几页开始（`home.filter` 全量续扫的断点）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT category_code, next_page FROM coin_filter_scan_progress"
        ).fetchall()
    return {row["category_code"]: int(row["next_page"]) for row in rows}


def set_coin_filter_scan_page(category_code: str, next_page: int) -> None:
    """写断点：1 表示从未深扫过；`filter_scan.DEEP_DONE_PAGE` 表示深处已扫到底。"""
    with db.connect() as conn, conn:
        conn.execute(
            "INSERT INTO coin_filter_scan_progress (category_code, next_page, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(category_code) DO UPDATE SET"
            " next_page = excluded.next_page, updated_at = excluded.updated_at",
            (category_code, next_page, now_local()),
        )


def count_uncovered_sold_by_category() -> dict[str, int]:
    """按 `category_code` 统计未回填出价人数（`bid_num IS NULL`）的成交拍品数（深扫配额权重）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT category_code, COUNT(*) AS n FROM coin_lots"
            " WHERE outcome = 'sold' AND bid_num IS NULL AND category_code IS NOT NULL"
            " GROUP BY category_code"
        ).fetchall()
    return {str(row["category_code"]): int(row["n"]) for row in rows}


def count_uncovered_sold_by_category_between(since: str, until: str) -> dict[str, int]:
    """按 `category_code` 统计 `[since, until)` 成交且缺出价人数（`bid_num IS NULL`）的拍品数。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT category_code, COUNT(*) AS n FROM coin_lots"
            " WHERE outcome = 'sold' AND bid_num IS NULL AND category_code IS NOT NULL"
            " AND closed_at >= ? AND closed_at < ?"
            " GROUP BY category_code",
            (since, until),
        ).fetchall()
    return {str(row["category_code"]): int(row["n"]) for row in rows}


def count_sold_by_category_since(since_utc: str) -> dict[str, int]:
    """按 `category_code` 统计 `since_utc` 之后的成交数（估算顶部窗口页数）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT category_code, COUNT(*) AS n FROM coin_lots"
            " WHERE outcome = 'sold' AND closed_at >= ? AND category_code IS NOT NULL"
            " GROUP BY category_code",
            (since_utc,),
        ).fetchall()
    return {str(row["category_code"]): int(row["n"]) for row in rows}


def coin_filter_scan_progress() -> dict[str, tuple[int, str]]:
    """各断点 key 的 `(next_page, updated_at)`：回补游标要按业务日重置。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT category_code, next_page, updated_at FROM coin_filter_scan_progress"
        ).fetchall()
    return {
        row["category_code"]: (int(row["next_page"]), str(row["updated_at"])) for row in rows
    }


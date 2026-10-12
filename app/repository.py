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

from app import db, models
from app.config import SourceConfig
from app.models import (
    CoinFetchRun,
    CoinLot,
    CoinLotCard,
    CoinOrder,
    CoinPriceRef,
    CoinProfileTag,
    CoinSession,
    CoinSessionCard,
    CoinSessionDetail,
    CoinShop,
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


def toggle_source_enabled(source_id: int) -> bool | None:
    """启停取反，返回新状态；源不存在返回 None（`/sources/{id}/toggle` 据此判 404）。"""
    with db.connect() as conn, conn:
        row = conn.execute("SELECT enabled FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            return None
        new_value = not bool(row["enabled"])
        conn.execute("UPDATE sources SET enabled = ? WHERE id = ?", (int(new_value), source_id))
        return new_value


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


# ---------------------------------------------------------------- 日报


def day_bounds_utc(tz: ZoneInfo, day: date) -> tuple[str, str]:
    """业务时区「某一天」的 `[start, end)`，折算成 UTC 存储格式。

    返回左闭右开区间，故上海 00:00:00 与 23:59:59 抓到的条目必然分属不同区间。
    """
    day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return to_utc_str(day_start), to_utc_str(day_start + timedelta(days=1))


def local_day_bounds(tz: ZoneInfo, *, now: datetime | None = None) -> tuple[str, str]:
    """算出业务时区「今天」的 `[start, end)`，折算成 UTC 存储格式（plan §9.1）。

    候选池用 `fetched_at` 而不是 `published_at`：日报要回答的是「**今天**新到的东西里
    哪些值得看」，用发布时间会漏掉「今天抓到的旧发布内容」（plan §12 第 7 条）。

    `now` 仅测试注入用。
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(tz)
    return day_bounds_utc(tz, moment.date())


def lookback_bounds(hours: int, *, now: datetime | None = None) -> tuple[str, str]:
    """当日无候选时的回退窗口：最近 `hours` 小时（页面需标注「非今日数据」）。"""
    moment = now or datetime.now(timezone.utc)
    return to_utc_str(moment - timedelta(hours=hours)), to_utc_str(moment)


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
            (utcnow(), item_id),
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
                utcnow(),
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
    now = utcnow()
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
    now = utcnow()
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
            (int(enabled), utcnow(), tag_id),
        )
        return cursor.rowcount > 0


def set_coin_tag_weight(tag_id: int, weight: float) -> bool:
    with db.connect() as conn, conn:
        cursor = conn.execute(
            "UPDATE coin_profile_tags SET weight = ?, updated_at = ? WHERE id = ?",
            (weight, utcnow(), tag_id),
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
                (mxiqi_id, shop_id, title, status, auction_at, auction_at_raw,
                 deposit, lot_total, view_count, cover_urls, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mxiqi_id) DO UPDATE SET
                shop_id        = excluded.shop_id,
                title          = excluded.title,
                status         = excluded.status,
                auction_at     = excluded.auction_at,
                auction_at_raw = excluded.auction_at_raw,
                deposit        = excluded.deposit,
                lot_total      = excluded.lot_total,
                view_count     = excluded.view_count,
                cover_urls     = excluded.cover_urls,
                fetched_at     = excluded.fetched_at
            """,
            (
                session.mxiqi_id,
                session.shop_id,
                session.title,
                session.status,
                session.auction_at,
                session.auction_at_raw,
                session.deposit,
                session.lot_total,
                session.view_count,
                _dump(session.cover_urls),
                session.fetched_at,
            ),
        )
        row = conn.execute(
            "SELECT id FROM coin_sessions WHERE mxiqi_id = ?", (session.mxiqi_id,)
        ).fetchone()
        return int(row["id"])


def upsert_coin_lot(lot: CoinLot) -> int:
    """按 `mxiqi_id` upsert，返回 `coin_lots.id`。

    匹配分与推荐位**只在首次入库时写**；后续重抓不覆盖已算过的匹配结果
    （匹配由 M9 的 matcher 单独更新，避免每次采集把推荐擦掉）。
    """
    with db.connect() as conn, conn:
        conn.execute(
            """
            INSERT INTO coin_lots
                (session_id, mxiqi_id, lot_no, title, start_price, current_price,
                 image_urls, tags, match_score, is_recommended, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mxiqi_id) DO UPDATE SET
                session_id    = excluded.session_id,
                lot_no        = excluded.lot_no,
                title         = excluded.title,
                start_price   = excluded.start_price,
                current_price = excluded.current_price,
                image_urls    = excluded.image_urls,
                tags          = excluded.tags,
                fetched_at    = excluded.fetched_at
            """,
            (
                lot.session_id,
                lot.mxiqi_id,
                lot.lot_no,
                lot.title,
                lot.start_price,
                lot.current_price,
                _dump(lot.image_urls),
                _dump(lot.tags),
                lot.match_score,
                int(lot.is_recommended),
                lot.fetched_at,
            ),
        )
        row = conn.execute(
            "SELECT id FROM coin_lots WHERE mxiqi_id = ?", (lot.mxiqi_id,)
        ).fetchone()
        return int(row["id"])


def update_coin_lot_match(lot_id: int, match_score: float, is_recommended: bool) -> None:
    """写回匹配结果（matcher 用）。"""
    with db.connect() as conn, conn:
        conn.execute(
            "UPDATE coin_lots SET match_score = ?, is_recommended = ? WHERE id = ?",
            (match_score, int(is_recommended), lot_id),
        )


def list_coin_lots_for_matching() -> list[CoinLot]:
    """全部拍品（含未推荐），供 matcher 重算匹配分。"""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM coin_lots ORDER BY id").fetchall()
    return [_row_to_coin_lot(row) for row in rows]


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
    """给页面顶部用的汇总：最近一次成功时间、最近一次是否被反爬拦下。

    `blocked` 取「最近一条日志」的状态：反爬是全局的，最新一轮被拦就该提示
    （spec §14.3 要求会话失效显著提示，不静默）。
    """
    with db.connect() as conn:
        last_ok = conn.execute(
            "SELECT MAX(finished_at) AS ts FROM coin_fetch_runs WHERE status = 'ok'"
        ).fetchone()["ts"]
        last = conn.execute("SELECT * FROM coin_fetch_runs ORDER BY id DESC LIMIT 1").fetchone()
        # 只看**最近一次查价**：登录成功后再跑，历史那条「要登录」的报错就不该继续提示
        price_row = conn.execute(
            "SELECT error FROM coin_fetch_runs WHERE kind = 'price_search' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    last_error = last["error"] if last else None
    return {
        "last_ok_at": last_ok,
        "last_status": last["status"] if last else None,
        "last_error": last_error,
        "blocked": bool(last) and last["status"] == "blocked",
        # 站内成交价要登录；用文本判定是为了不动 status 取值（页面据此提示去登录）
        "needs_login": bool(price_row) and "登录" in (price_row["error"] or ""),
        "login_error": price_row["error"] if price_row else None,
    }


# ---------------------------------------------------------------- 页面查询


_SESSION_CARD_SQL = """
    SELECT s.id AS session_id, s.mxiqi_id, s.title, s.status,
           s.auction_at, s.auction_at_raw, s.lot_total, s.view_count, s.cover_urls,
           sh.name AS shop_name, sh.avatar_url AS shop_avatar,
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
    )


def list_coin_session_cards(limit: int | None = None) -> list[CoinSessionCard]:
    """第一页：**只列出至少命中 1 条推荐拍品的专场**（spec §14.7「没有就不展示」）。

    未命中的专场仍全量入库，只是不在这里出现 —— 画像改动后可以重新匹配，不必重抓。
    """
    sql = (
        _SESSION_CARD_SQL
        + """
    WHERE (SELECT COUNT(*) FROM coin_lots l
            WHERE l.session_id = s.id AND l.is_recommended = 1) > 0
    ORDER BY (s.auction_at IS NULL), s.auction_at DESC, s.id DESC
    """
    )
    params: list[object] = []
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
            WHERE session_id = ? AND is_recommended = 1
            ORDER BY match_score DESC, (lot_no IS NULL), lot_no, id
            """,
            (session_id,),
        ).fetchall()
        ref_rows = conn.execute(
            """
            SELECT * FROM coin_price_refs
            WHERE lot_id IN (SELECT id FROM coin_lots WHERE session_id = ? AND is_recommended = 1)
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
        auction_at=session["auction_at"],
        auction_at_raw=session["auction_at_raw"],
        deposit=session["deposit"],
        lot_total=session["lot_total"],
        view_count=session["view_count"],
        recommended_count=len(lots),
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


"""数据表 DDL 与行对象定义。"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = "1"

# 所有时间列存 UTC ISO8601 字符串（SQLite 无原生时间类型）
DDL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sources (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL,
  url         TEXT    NOT NULL UNIQUE,
  module      TEXT    NOT NULL CHECK (module IN ('agent','bigdata')),
  enabled     INTEGER NOT NULL DEFAULT 1,
  last_ok_at  TEXT,
  fail_count  INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  module       TEXT    NOT NULL,
  title        TEXT    NOT NULL,
  url          TEXT    NOT NULL,
  url_hash     TEXT    NOT NULL UNIQUE,
  published_at TEXT,
  summary      TEXT,
  fetched_at   TEXT    NOT NULL,
  clicked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_fetched  ON items(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_module   ON items(module, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_clicked  ON items(clicked_at) WHERE clicked_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS item_scores (
  item_id        INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  score          REAL    NOT NULL CHECK (score >= 0 AND score <= 100),
  reason         TEXT    NOT NULL,
  model          TEXT,
  prompt_version TEXT    NOT NULL,
  scored_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_score ON item_scores(score DESC);

CREATE TABLE IF NOT EXISTS fetch_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  started_at  TEXT    NOT NULL,
  finished_at TEXT,
  status      TEXT    NOT NULL,
  http_status INTEGER,
  items_new   INTEGER DEFAULT 0,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_source_time ON fetch_runs(source_id, started_at DESC);

CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class FetchStatus:
    """fetch_runs.status 取值。"""

    OK = "ok"
    EMPTY = "empty"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    PARSE_ERROR = "parse_error"


@dataclass
class Source:
    id: int
    name: str
    url: str
    module: str
    enabled: bool
    last_ok_at: str | None
    fail_count: int
    created_at: str


@dataclass
class Item:
    id: int | None
    source_id: int
    module: str
    title: str
    url: str
    url_hash: str
    published_at: str | None
    summary: str | None
    fetched_at: str
    clicked_at: str | None = None


@dataclass
class ItemScore:
    item_id: int
    score: float
    reason: str
    model: str | None
    prompt_version: str
    scored_at: str


@dataclass
class ScoreCandidate:
    """待打分池里的一条（items LEFT JOIN sources），已带 prompt 需要的字段。"""

    item_id: int
    module: str
    title: str
    summary: str | None
    published_at: str | None
    source_name: str


@dataclass
class DigestItem:
    """日报/列表页的一条（items + 源名 + 打分），模板直接渲染这个对象。

    `score` / `reason` 为 None 表示这条还没打上分（模板显示「未评分」，plan §9.2）。
    """

    item_id: int
    module: str
    title: str
    url: str
    source_name: str
    published_at: str | None
    fetched_at: str
    score: float | None
    reason: str | None


@dataclass
class FetchRun:
    id: int | None
    source_id: int
    started_at: str
    finished_at: str | None
    status: str
    http_status: int | None
    items_new: int
    error: str | None = None

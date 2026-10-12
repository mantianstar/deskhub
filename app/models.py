"""数据表 DDL 与行对象定义。"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = "2"

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

-- ============ 铜币拍卖（spec §14 / plan §18）============
-- 与上面的知识聚合表无外键关联：数据形态（专场/拍品）与 RSS 条目完全不同，硬塞会弄脏两边。

-- 历史订单解析结果（画像的原料）
CREATE TABLE IF NOT EXISTS coin_orders (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no     TEXT    NOT NULL,
  shop_name    TEXT,
  item_name    TEXT    NOT NULL,
  price        REAL,
  country      TEXT,
  era          TEXT,
  denomination TEXT,
  category     TEXT,
  condition    TEXT,
  parsed_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_coin_orders ON coin_orders(order_no, item_name);

-- 画像结构化标签（可编辑）
CREATE TABLE IF NOT EXISTS coin_profile_tags (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  dimension  TEXT    NOT NULL CHECK (dimension IN
    ('country','era','denomination','category','condition','shop','price_band')),
  value      TEXT    NOT NULL,
  weight     REAL    NOT NULL DEFAULT 1,
  hit_count  INTEGER NOT NULL DEFAULT 0,
  enabled    INTEGER NOT NULL DEFAULT 1,
  note       TEXT,
  created_at TEXT    NOT NULL,
  updated_at TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_coin_tags ON coin_profile_tags(dimension, value);

-- 店铺
CREATE TABLE IF NOT EXISTS coin_shops (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  mxiqi_uid  TEXT    NOT NULL UNIQUE,
  name       TEXT,
  avatar_url TEXT,
  badges     TEXT,          -- JSON 数组
  fetched_at TEXT
);

-- 专场
CREATE TABLE IF NOT EXISTS coin_sessions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  mxiqi_id       TEXT    NOT NULL UNIQUE,
  shop_id        INTEGER REFERENCES coin_shops(id) ON DELETE SET NULL,
  title          TEXT    NOT NULL,
  status         TEXT,      -- preview | running | closed
  auction_at     TEXT,      -- UTC ISO8601
  auction_at_raw TEXT,      -- 站点原始文案
  deposit        REAL,
  lot_total      INTEGER,
  view_count     INTEGER,
  cover_urls     TEXT,      -- JSON 数组（列表页 4 图）
  fetched_at     TEXT    NOT NULL
);

-- 拍品
CREATE TABLE IF NOT EXISTS coin_lots (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id     INTEGER NOT NULL REFERENCES coin_sessions(id) ON DELETE CASCADE,
  mxiqi_id       TEXT    NOT NULL UNIQUE,
  lot_no         TEXT,
  title          TEXT    NOT NULL,
  start_price    REAL,
  current_price  REAL,
  image_urls     TEXT,    -- JSON 数组（页面展示 ≤3）
  tags           TEXT,    -- JSON 对象：{"发行年份":"1808","国家地区":"印度"}
  match_score    REAL    NOT NULL DEFAULT 0,
  is_recommended INTEGER NOT NULL DEFAULT 0,
  fetched_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_lots_rec
  ON coin_lots(session_id, is_recommended, match_score DESC);

-- 推荐价的取证记录
CREATE TABLE IF NOT EXISTS coin_price_refs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  lot_id       INTEGER NOT NULL REFERENCES coin_lots(id) ON DELETE CASCADE,
  keyword      TEXT    NOT NULL,
  sample_count INTEGER NOT NULL,
  p25          REAL,
  median       REAL,
  p75          REAL,
  raw          TEXT,      -- JSON：命中的成交（标题 + 价格）
  queried_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_refs_lot ON coin_price_refs(lot_id);

-- 采集日志
CREATE TABLE IF NOT EXISTS coin_fetch_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT    NOT NULL,   -- sessions | session_detail | price_search
  target      TEXT,
  started_at  TEXT    NOT NULL,
  finished_at TEXT,
  status      TEXT    NOT NULL,   -- ok | blocked | http_error | parse_error | timeout
  http_status INTEGER,
  items_new   INTEGER DEFAULT 0,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_coin_runs ON coin_fetch_runs(kind, started_at DESC);
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


# ---------------------------------------------------------------- 铜币拍卖


class CoinFetchStatus:
    """`coin_fetch_runs.status` 取值；比 `FetchStatus` 多一个 `blocked`。

    `blocked` 专指「被麦稀奇反爬拦下（滑块会话失效）」—— 它要显著提示而不是当普通错误，
    所以单列一个状态（spec §14.3）。
    """

    OK = "ok"
    BLOCKED = "blocked"
    HTTP_ERROR = "http_error"
    PARSE_ERROR = "parse_error"
    TIMEOUT = "timeout"


# 画像维度（与 coin_profile_tags.dimension 的 CHECK 一致）
COIN_PROFILE_DIMENSIONS = (
    "country",
    "era",
    "denomination",
    "category",
    "condition",
    "shop",
    "price_band",
)

# 维度中文名（画像页与 CLI 共用；纯展示数据，放这里避免 CLI 去 import router）
COIN_DIMENSION_LABELS = {
    "country": "国家/地区",
    "era": "年代",
    "denomination": "面值",
    "category": "品类",
    "condition": "品相/包浆",
    "shop": "店铺",
    "price_band": "价位区间",
}

# 专场状态 → 中文（麦稀奇列表页的三种状态）
COIN_SESSION_STATUS_LABELS = {
    "preview": "预出价",
    "running": "进行中",
    "closed": "已结束",
}


@dataclass
class CoinOrder:
    """`orders.xlsx` 的一行 + LLM 抽出的结构化字段。"""

    order_no: str
    shop_name: str | None
    item_name: str
    price: float | None
    country: str | None = None
    era: str | None = None
    denomination: str | None = None
    category: str | None = None
    condition: str | None = None


@dataclass
class CoinProfileTag:
    id: int | None
    dimension: str
    value: str
    weight: float
    hit_count: int
    enabled: bool
    note: str | None
    created_at: str
    updated_at: str


@dataclass
class CoinShop:
    id: int | None
    mxiqi_uid: str
    name: str | None
    avatar_url: str | None
    badges: list[dict]
    fetched_at: str | None


@dataclass
class CoinSession:
    id: int | None
    mxiqi_id: str
    shop_id: int | None
    title: str
    status: str | None
    auction_at: str | None
    auction_at_raw: str | None
    deposit: float | None
    lot_total: int | None
    view_count: int | None
    cover_urls: list[str]
    fetched_at: str


@dataclass
class CoinLot:
    id: int | None
    session_id: int
    mxiqi_id: str
    lot_no: str | None
    title: str
    start_price: float | None
    current_price: float | None
    image_urls: list[str]
    tags: dict[str, str]
    match_score: float
    is_recommended: bool
    fetched_at: str


@dataclass
class CoinPriceRef:
    lot_id: int
    keyword: str
    sample_count: int
    p25: float | None
    median: float | None
    p75: float | None
    raw: str | None
    queried_at: str


@dataclass
class CoinFetchRun:
    id: int | None
    kind: str
    target: str | None
    started_at: str
    finished_at: str | None
    status: str
    http_status: int | None
    items_new: int
    error: str | None = None


@dataclass
class CoinSessionCard:
    """第一页（专场列表）的一张卡：专场 + 店铺 + 命中数。"""

    session_id: int
    mxiqi_id: str
    title: str
    status: str | None
    auction_at: str | None
    auction_at_raw: str | None
    lot_total: int | None
    view_count: int | None
    cover_urls: list[str]
    shop_name: str | None
    shop_avatar: str | None
    recommended_count: int


@dataclass
class CoinLotCard:
    """第二页（拍品页）的一条推荐拍品：拍品 + 推荐价取证。"""

    lot_id: int
    lot_no: str | None
    title: str
    start_price: float | None
    current_price: float | None
    image_urls: list[str]
    tags: dict[str, str]
    match_score: float
    keyword: str | None
    sample_count: int | None
    p25: float | None
    median: float | None
    p75: float | None


@dataclass
class CoinSessionDetail:
    """第二页的整页数据：店铺头 + 推荐拍品列表。"""

    session_id: int
    mxiqi_id: str
    title: str
    status: str | None
    auction_at: str | None
    auction_at_raw: str | None
    deposit: float | None
    lot_total: int | None
    view_count: int | None
    recommended_count: int
    shop: CoinShop | None
    lots: list[CoinLotCard]

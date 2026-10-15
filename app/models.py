"""数据表 DDL 与行对象定义。"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = "6"

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
  auction_at     TEXT,      -- 拍卖时间（站内口径 = 截拍 end_time）的 UTC ISO8601
  auction_at_raw TEXT,      -- 截拍时间的站点文案，如「10月12日 星期一 19:30」
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
  status      TEXT    NOT NULL,   -- ok | http_error | parse_error | timeout
  http_status INTEGER,
  items_new   INTEGER DEFAULT 0,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_coin_runs ON coin_fetch_runs(kind, started_at DESC);

-- ============ 铜币参考库（Numista，v1.2 增补）============
-- 与「拍卖」那批表无外键关联：这是**硬币本身**的目录信息（系列/单版），来源是 numista.com，
-- 用于给拍品提供权威的币种背景（材质、尺寸、发行量、目录号…），不是麦稀奇的拍品。

-- 系列（Numista type）：一个币种（国家 + 面值 + 执政者/版式），跨多个年份
CREATE TABLE IF NOT EXISTS coin_series (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  numista_id        INTEGER NOT NULL UNIQUE,   -- 系列号（Numista type id）
  title             TEXT,                      -- 系列名称
  url               TEXT,                      -- 网页链接
  object_type       TEXT,                      -- 大类（流通币/纪念币/代币…）
  issuer            TEXT,                      -- 国家/发行方
  min_year          INTEGER,                   -- 系列起始年份
  max_year          INTEGER,                   -- 系列结束年份
  ruler             TEXT,                      -- 执政者
  denomination      TEXT,                      -- 面值文本
  size_mm           REAL,                      -- 尺寸（直径 mm）
  weight_g          REAL,                      -- 重量（g）—— 单版继承用，附录产出不列
  thickness_mm      REAL,                      -- 厚度（mm）
  shape             TEXT,                      -- 形状
  composition       TEXT,                      -- 材质
  edge              TEXT,                      -- 币边描述 —— 单版继承用
  obverse_desc      TEXT,                      -- 正面描述
  obverse_lettering TEXT,                      -- 正面铭文
  obverse_picture   TEXT,                      -- 正面图片
  reverse_desc      TEXT,                      -- 背面描述
  reverse_lettering TEXT,                      -- 背面铭文
  reverse_picture   TEXT,                      -- 背面图片
  catalog_no        TEXT,                      -- 目录号（全部引用，如 KM#790,Sp#3961）
  comments          TEXT,                      -- 系列备注（Numista type 的 comments）
  mints             TEXT,                      -- 造币厂（JSON 数组，如 ["Royal Mint (Tower Hill)"]）
  fetched_at        TEXT    NOT NULL
);

-- 系列中文版：字段个数与 coin_series **完全一致**，可翻译字段存中文，其余原样复制
CREATE TABLE IF NOT EXISTS coin_series_cn (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  numista_id        INTEGER NOT NULL UNIQUE,
  title             TEXT,
  url               TEXT,
  object_type       TEXT,
  issuer            TEXT,
  min_year          INTEGER,
  max_year          INTEGER,
  ruler             TEXT,
  denomination      TEXT,
  size_mm           REAL,
  weight_g          REAL,
  thickness_mm      REAL,
  shape             TEXT,
  composition       TEXT,
  edge              TEXT,
  obverse_desc      TEXT,
  obverse_lettering TEXT,
  obverse_picture   TEXT,
  reverse_desc      TEXT,
  reverse_lettering TEXT,
  reverse_picture   TEXT,
  catalog_no        TEXT,
  comments          TEXT,
  mints             TEXT,
  fetched_at        TEXT    NOT NULL
);

-- 单版（Numista issue）：某系列下的一个年份/厂别条目
CREATE TABLE IF NOT EXISTS coin_issues (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  numista_id   INTEGER NOT NULL UNIQUE,   -- 单版编号（Numista issue id）
  series_id    INTEGER NOT NULL REFERENCES coin_series(id) ON DELETE CASCADE,
  year         INTEGER,                   -- 年份
  mint         TEXT,                      -- 造币厂
  mint_mark    TEXT,                      -- 厂标
  variety      TEXT,                      -- 版别备注
  comment      TEXT,                      -- 补充备注
  mintage      INTEGER,                   -- 发行量
  size_mm      REAL,                      -- 尺寸（直径 mm，继承系列）
  weight_g     REAL,                      -- 重量（g，继承系列）
  thickness_mm REAL,                      -- 厚度（mm，继承系列）
  edge         TEXT,                      -- 币边（继承系列）
  is_proof     INTEGER,                   -- 是否精制（1/0/NULL=未知）
  is_essai     INTEGER,                   -- 是否样币（1/0/NULL=未知）
  picture      TEXT,                      -- 图片
  catalog_no   TEXT,                      -- 目录号（该单版的 references，如 Sp#3961）
  fetched_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_issues_series ON coin_issues(series_id, year);

-- 单版中文版：字段个数与 coin_issues **完全一致**，mint/comment/edge 存中文
CREATE TABLE IF NOT EXISTS coin_issues_cn (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  numista_id   INTEGER NOT NULL UNIQUE,
  series_id    INTEGER NOT NULL REFERENCES coin_series_cn(id) ON DELETE CASCADE,
  year         INTEGER,
  mint         TEXT,
  mint_mark    TEXT,
  variety      TEXT,
  comment      TEXT,
  mintage      INTEGER,
  size_mm      REAL,
  weight_g     REAL,
  thickness_mm REAL,
  edge         TEXT,
  is_proof     INTEGER,
  is_essai     INTEGER,
  picture      TEXT,
  catalog_no   TEXT,
  fetched_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_issues_cn_series ON coin_issues_cn(series_id, year);

-- 单版的**估值**与 **Frequency**（v1.4 增补）—— 来源是 Numista 类型页的表格，
-- 不是接口：`/types/{id}/issues` 里这两项都没有（见 plan §18.13.2）。
-- 与 coin_issues 用 `numista_id` 对齐（不设外键：页面先于/独立于接口采到也不丢数据）。
CREATE TABLE IF NOT EXISTS coin_issue_values (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  numista_id        INTEGER NOT NULL UNIQUE,   -- 单版编号（Numista issue id，对应 coin_issues.numista_id）
  series_numista_id INTEGER NOT NULL,          -- 所属系列号（Numista type id）
  currency          TEXT,                      -- 估值币种（页面标注，实测本机是 CNY）
  value_g           REAL,                      -- 各品相估值（页面口径；缺该品相留空）
  value_vg          REAL,
  value_f           REAL,
  value_vf          REAL,
  value_xf          REAL,
  value_au          REAL,
  value_unc         REAL,
  frequency         REAL,                      -- Frequency：拥有该年份/品种的用户占比（百分数，0.16 = 0.16%）
  fetched_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_issue_values_series ON coin_issue_values(series_numista_id);

-- Numista 调用计数（免费额度每月 2000 次，需自管风控；Numista 响应不带限流头）
CREATE TABLE IF NOT EXISTS coin_api_usage (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  provider  TEXT    NOT NULL,   -- 目前只有 numista
  called_at TEXT    NOT NULL    -- UTC ISO8601
);
CREATE INDEX IF NOT EXISTS idx_coin_api_usage ON coin_api_usage(provider, called_at);

-- ============ 功能开关（管理页 /control）============
-- 控制「各类任务是否自动执行」：关掉后每天 08:00 的定时任务不再跑它。
-- 页面上的「立即运行」是手动触发，忽略这张表（见 app/switches.py）。
-- 启动补拉已取消：重启本身不执行任何任务，会读这张表的只有定时任务。
CREATE TABLE IF NOT EXISTS feature_switches (
  key        TEXT PRIMARY KEY,   -- knowledge_fetch / knowledge_score / coins_collect / coins_price
  enabled    INTEGER NOT NULL,
  updated_at TEXT    NOT NULL    -- UTC ISO8601
);
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
    """`coin_fetch_runs.status` 取值（与 `FetchStatus` 同义，单独列出便于铜币模块内引用）。"""

    OK = "ok"
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
class CoinSeries:
    """Numista 系列（一个币种）。字段口径见 spec §14.13 / plan §18.13。"""

    id: int | None
    numista_id: int
    title: str | None
    url: str | None
    object_type: str | None
    issuer: str | None
    min_year: int | None
    max_year: int | None
    ruler: str | None
    denomination: str | None
    size_mm: float | None
    weight_g: float | None
    thickness_mm: float | None
    shape: str | None
    composition: str | None
    edge: str | None
    obverse_desc: str | None
    obverse_lettering: str | None
    obverse_picture: str | None
    reverse_desc: str | None
    reverse_lettering: str | None
    reverse_picture: str | None
    catalog_no: str | None
    comments: str | None
    mints: list[str]
    fetched_at: str


@dataclass
class CoinIssue:
    """Numista 单版（系列下的一个年份/厂别条目）。"""

    id: int | None
    numista_id: int
    series_id: int
    year: int | None
    mint: str | None
    mint_mark: str | None
    variety: str | None
    comment: str | None
    mintage: int | None
    size_mm: float | None
    weight_g: float | None
    thickness_mm: float | None
    edge: str | None
    is_proof: bool | None
    is_essai: bool | None
    picture: str | None
    catalog_no: str | None
    fetched_at: str


@dataclass
class CoinIssueValues:
    """单版的估值（各品相）+ Frequency。来源是 Numista 类型页表格，见 plan §18.13.2。"""

    id: int | None
    numista_id: int
    series_numista_id: int
    currency: str | None
    value_g: float | None
    value_vg: float | None
    value_f: float | None
    value_vf: float | None
    value_xf: float | None
    value_au: float | None
    value_unc: float | None
    frequency: float | None
    fetched_at: str


@dataclass(frozen=True)
class CoinSeriesIssueStats:
    """某系列已采集单版的统计：**不同年份数** + **版别（单版）数**。

    两者不可混为一谈：一个年份可能有多个版别（厂标 / 变体 / 精制），
    故「1895–1901 应该有 7 个年份、15 个版别」，不能把单版条数当成年份数。
    """

    year_count: int
    issue_count: int


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

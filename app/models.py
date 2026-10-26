"""数据表 DDL 与行对象定义。"""

from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = "22"

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
  followed   INTEGER NOT NULL DEFAULT 0,  -- 1 = 已关注店铺
  fetched_at TEXT
);

-- 专场
CREATE TABLE IF NOT EXISTS coin_sessions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  mxiqi_id       TEXT    NOT NULL UNIQUE,
  shop_id        INTEGER REFERENCES coin_shops(id) ON DELETE SET NULL,
  title          TEXT    NOT NULL,
  status         TEXT,      -- preview | running | closed
  kind           TEXT,      -- live（实时，展示开拍时间）| timed（限时，展示截拍时间）
  auction_at     TEXT,      -- 展示时间：实时 = 开拍 start_time，限时 = 截拍 end_time（UTC ISO8601）
  auction_at_raw TEXT,      -- 展示时间的站点文案，如「10月12日 星期一 19:30」
  deposit        REAL,
  commission_rate REAL,      -- 佣金费率（0.03 = 3%）
  commission_min  REAL,      -- 最低佣金（元）
  express_desc    TEXT,      -- 运费说明（站点富文本，原样存）
  express_day     INTEGER,   -- 发货天数
  express_fee     REAL,      -- 基础运费（元；从 express_desc 抽首个「运费 ¥N」，忽略地区差）
  express_free_amount REAL,  -- 满额包邮门槛（元）
  is_storage      INTEGER NOT NULL DEFAULT 0,  -- 是否可寄存（拍下不发货、先存店）
  lot_total      INTEGER,
  view_count     INTEGER,
  cover_urls     TEXT,      -- JSON 数组（列表页 4 图）
  fetched_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_sessions_status ON coin_sessions(status, auction_at);

-- 拍品分类字典（站点原始码 → 名称；v21）
CREATE TABLE IF NOT EXISTS coin_categories (
  code       TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  sort       INTEGER NOT NULL DEFAULT 0,
  fetched_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
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
  outcome        TEXT,    -- 终态：sold（成交）| unsold（未成交）；NULL = 在拍 / 未回采
  final_price    REAL,    -- 成交价（元）；未成交为 NULL
  winner_uid     TEXT,    -- 中拍人站内 uid（仅成交）
  closed_at      TEXT,    -- 终态时间（UTC ISO8601，取自拍品 end_time）
  image_urls     TEXT,    -- JSON 数组（页面展示 ≤3）
  tags           TEXT,    -- JSON 对象：{"发行年份":"1808","国家地区":"印度"}
  category_code  TEXT REFERENCES coin_categories(code) ON DELETE SET NULL,  -- 站点分类原始码（v21）
  is_graded      INTEGER,  -- 1 = 评级盒（标题正则），0 = 裸币；NULL 视作裸币（v22）
  series_numista_id INTEGER,  -- 匹配到的 Numista 系列号（plan §18.18.5）
  issue_numista_id  INTEGER,  -- 唯一命中的单版号；多候选 / 无命中为空
  match_method   TEXT,    -- attrs / title（后缀 _multi = 多候选未落库）；NULL = 未映射
  confidence     REAL,    -- 0..1；多候选时为 1/候选数
  match_candidates TEXT,  -- JSON 数组：歧义候选的 Numista 编号（系列级或单版级）
  match_score    REAL    NOT NULL DEFAULT 0,
  is_recommended INTEGER NOT NULL DEFAULT 0,
  -- 热度：只有 ListAuction 预览 / home.filter 带，auction.get 没有（plan §18.18.1 P1-6）
  bid_num        INTEGER,
  bid_user_num   INTEGER,
  bid_source     TEXT,    -- preview | home_filter；NULL = 没有来源给过值
  fetched_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_lots_rec
  ON coin_lots(session_id, is_recommended, match_score DESC);

-- 拍品图片的 OSS key（同图识别用，plan §18.18.1 P1-7）
CREATE TABLE IF NOT EXISTS coin_lot_images (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  lot_id  INTEGER NOT NULL REFERENCES coin_lots(id) ON DELETE CASCADE,
  oss_key TEXT    NOT NULL,
  UNIQUE(lot_id, oss_key)
);
CREATE INDEX IF NOT EXISTS idx_coin_lot_images_key ON coin_lot_images(oss_key);
CREATE INDEX IF NOT EXISTS idx_coin_lot_images_lot ON coin_lot_images(lot_id);

-- 全局拍品搜索索引（FTS5；CJK 逐字分词见 app/textmatch.fts_text）
CREATE VIRTUAL TABLE IF NOT EXISTS coin_lot_fts USING fts5(
  search_text,
  tokenize = 'unicode61'
);
CREATE TRIGGER IF NOT EXISTS coin_lots_fts_delete
AFTER DELETE ON coin_lots BEGIN
  DELETE FROM coin_lot_fts WHERE rowid = old.id;
END;

-- 推荐价的取证记录
CREATE TABLE IF NOT EXISTS coin_price_refs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  lot_id       INTEGER NOT NULL REFERENCES coin_lots(id) ON DELETE CASCADE,
  keyword      TEXT    NOT NULL,
  sample_count INTEGER NOT NULL,
  p25          REAL,
  median       REAL,
  p75          REAL,
  raw          TEXT,      -- JSON：命中的成交（item_id + 标题 + 价格 + 成交时间）
  queried_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_refs_lot ON coin_price_refs(lot_id);

-- 成交明细（可回测的价格基准库；见 plan/coins.md §18.18）
CREATE TABLE IF NOT EXISTS coin_sales (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id    TEXT    NOT NULL UNIQUE,   -- 站内拍品 id（去重键，不用标题）
  title      TEXT    NOT NULL,
  price      REAL    NOT NULL,          -- 成交价（元）
  sold_at    TEXT,                      -- 成交时间（UTC ISO8601）
  session_id INTEGER REFERENCES coin_sessions(id) ON DELETE SET NULL,
  shop_id    INTEGER REFERENCES coin_shops(id) ON DELETE SET NULL,
  keyword    TEXT,                      -- 仅 sold_search 来源有
  source     TEXT    NOT NULL,          -- session_final（高置信）| sold_search（中置信）
  fetched_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_sales_source ON coin_sales(source);
CREATE INDEX IF NOT EXISTS idx_coin_sales_sold_at ON coin_sales(sold_at);

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
  year         INTEGER,                   -- 年份（Numista 原值，如明治 6 年 = 6）
  gregorian_year INTEGER,                 -- 公历年份（与 year 不一致时页面并显）
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
  gregorian_year INTEGER,
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

-- 历史购买品相录入（人工按「国家 + 年份 + 面值」组合录一档品相，spec §14.16）
-- 独立于 coin_orders：那是订单级原料，这里是一个币种组合一行，不覆盖 LLM 抽的 condition。
CREATE TABLE IF NOT EXISTS coin_purchase_grades (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  country      TEXT NOT NULL,
  era          TEXT NOT NULL,   -- 年份 / 年号（与 coin_orders.era 同源）
  denomination TEXT NOT NULL,
  grade        TEXT NOT NULL CHECK (grade IN ('好','普通','差')),
  updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_coin_purchase_grades
  ON coin_purchase_grades(country, era, denomination);

-- 收藏系列（spec §14.17）：Numista 系列划分过细，把「同执政者同面值、尺寸差 ≤ 2mm」的
-- 多个 Numista series 手工合并成一个收藏系列。`collection_key` 是自定义 series id。
CREATE TABLE IF NOT EXISTS coin_collection_series (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  collection_key TEXT    NOT NULL UNIQUE,
  name           TEXT    NOT NULL,          -- 国家 · 执政者 · 面值
  country        TEXT    NOT NULL,
  ruler          TEXT,
  denomination   TEXT    NOT NULL,
  size_mm        REAL,                      -- 代表尺寸（成员系列尺寸中位数）
  size_band      TEXT,                      -- 小 / 中 / 大 / 超大
  created_at     TEXT    NOT NULL,
  updated_at     TEXT    NOT NULL
);

-- 收藏系列 ↔ Numista 系列的手工映射（一个 Numista series 只能属于一个收藏系列）
CREATE TABLE IF NOT EXISTS coin_collection_series_members (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  collection_id       INTEGER NOT NULL REFERENCES coin_collection_series(id) ON DELETE CASCADE,
  numista_series_id   INTEGER NOT NULL UNIQUE,
  numista_series_name TEXT,
  created_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_collection_members
  ON coin_collection_series_members(collection_id);

-- 跨系列探索候选池（spec §14.17.6）：扫描 Numista 目录得到的外部铜币系列。
-- 采纳前只存在这张表里，不写 coin_series，避免污染信息页 / 采集管理页。
CREATE TABLE IF NOT EXISTS coin_explore_candidates (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  numista_id  INTEGER NOT NULL UNIQUE,     -- Numista 系列号
  seed_key    TEXT,                        -- 来源种子：收藏系列的 collection_key
  seed_name   TEXT,                        -- 来源种子展示名（国家 · 执政者 · 面值）
  size_diff   REAL,                        -- 与来源种子的尺寸差（mm）
  status      TEXT    NOT NULL,            -- candidate / adopted / ignored
  payload     TEXT    NOT NULL,            -- SeriesInfo JSON（英文）
  payload_cn  TEXT    NOT NULL,            -- SeriesInfo JSON（中文，页面与采纳用）
  issues      TEXT    NOT NULL,            -- IssueInfo JSON 数组（P4/P5 候选缺失年份用）
  verified_at TEXT    NOT NULL,
  created_at  TEXT    NOT NULL,
  updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_explore_status
  ON coin_explore_candidates(status);

-- ============ 功能开关（管理页 /control）============
-- 控制「各类任务是否自动执行」：关掉后每天 07:00 的定时任务不再跑它。
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
    "closed": "已截拍",
}

# 历史购买品相录入的可选档位（coin_purchase_grades.grade 的 CHECK 一致）
COIN_PURCHASE_GRADES = ("好", "普通", "差")


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
class CoinOrderCombo:
    """历史订单按「国家 + 年份 + 面值」去重后的一行（品相录入页的展示单元）。

    `order_count` 是该组合下的订单数；`grade` 是人工已录的品相（未录为 None）。
    """

    country: str
    era: str
    denomination: str
    order_count: int
    grade: str | None
    # 派生展示字段（不落库）：命中的收藏系列名，按国家 + 面值匹配
    collection_name: str = ""
    # 派生展示字段（不落库）：日本年号的「年号 + 年号年份」（如 明治六年），无公历年为空
    era_label: str = ""


@dataclass
class CoinPurchaseGrade:
    """人工录入的一条品相（`(country, era, denomination)` 唯一）。"""

    id: int | None
    country: str
    era: str
    denomination: str
    grade: str
    updated_at: str


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
    followed: bool = False


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
    # live（实时）/ timed（限时）；老库迁移前为空
    kind: str | None = None
    # 费率与到手价（plan §18.18.1 P1-8）
    commission_rate: float | None = None
    commission_min: float | None = None
    express_desc: str | None = None
    express_day: int | None = None
    # 基础运费 / 满额包邮门槛（元）；老库与无运费说明的场次为空
    express_fee: float | None = None
    express_free_amount: float | None = None
    is_storage: bool = False


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
    # 终态（截拍后回采，见 plan/coins.md §18.18）：在拍 / 未回采时为 None
    outcome: str | None = None
    final_price: float | None = None
    winner_uid: str | None = None
    closed_at: str | None = None
    # 热度与同图识别（plan/coins.md §18.18.1 P1-6 / P1-7）
    bid_num: int | None = None
    bid_user_num: int | None = None
    bid_source: str | None = None
    image_keys: list[str] = field(default_factory=list)
    # 站点分类：取最细一级（`cate_id_2` 优先、回落 `cate_id_1`；plan §18.19.4）
    category_code: str | None = None
    # 评级盒判定（标题正则；1 = 评级盒，0 = 裸币；plan §18.19.6）
    is_graded: int | None = None


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
class CoinSale:
    """一条成交明细；`source` 高置信（session_final）不会被中置信覆盖。"""

    id: int | None
    item_id: str
    title: str
    price: float
    sold_at: str | None
    session_id: int | None
    shop_id: int | None
    keyword: str | None
    source: str
    fetched_at: str


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
    gregorian_year: int | None
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


COIN_EXPLORE_STATUSES = ("candidate", "adopted", "ignored")
COIN_EXPLORE_STATUS_LABELS = {
    "candidate": "候选",
    "adopted": "采纳",
    "ignored": "忽略",
}


@dataclass
class CoinExploreCandidate:
    """跨系列探索候选（spec §14.17.6）。payload 保存 Numista 系列详情，采纳时才写入 coin_series。"""

    id: int | None
    numista_id: int
    seed_key: str | None
    seed_name: str | None
    size_diff: float | None
    status: str
    payload: dict[str, object]
    payload_cn: dict[str, object]
    issues: list[dict[str, object]]
    verified_at: str
    created_at: str
    updated_at: str


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
    precise_count: int = 0
    similar_count: int = 0
    shop_followed: bool = False
    # live（实时）/ timed（限时）
    kind: str | None = None


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
    is_recommended: bool = False
    # 终态（截拍后回采）：sold / unsold；在拍或未回采时为 None
    outcome: str | None = None
    final_price: float | None = None


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
    # live（实时）/ timed（限时）
    kind: str | None = None


# ---------------------------------------------------------------- 收藏画像（spec §14.17）


@dataclass
class CoinCollectionSeries:
    """合并后的收藏系列（多个 Numista series → 一个自定义 series）。"""

    id: int | None
    collection_key: str
    name: str
    country: str
    ruler: str | None
    denomination: str
    size_mm: float | None
    size_band: str | None
    created_at: str
    updated_at: str


@dataclass
class CoinCollectionMember:
    """收藏系列 ↔ Numista 系列的手工映射。"""

    id: int | None
    collection_id: int
    numista_series_id: int
    numista_series_name: str | None
    created_at: str


@dataclass(frozen=True)
class CoinYearSpec:
    """画像「年份规格字典」里的一年（同一年多个普制版别时取发行量最大的）。"""

    year: int
    gregorian_year: int | None
    numista_issue_id: int
    mintage: int | None
    rarity: float | None          # Numista Frequency / 100，0.006 = 0.6%
    size_mm: float | None
    weight_g: float | None
    price_vf: float | None
    price_xf: float | None
    currency: str | None
    is_proof: bool | None
    comment: str | None = None      # 单版备注（`coin_issues_cn.comment`）

    def as_dict(self) -> dict:
        return {
            "mintage": self.mintage,
            "rarity": self.rarity,
            "size": self.size_mm,
            "weight": self.weight_g,
            "price_estimates": {"VF": self.price_vf, "XF": self.price_xf},
            "comment": self.comment,
        }


@dataclass
class CoinCollectionProfile:
    """一个收藏系列的画像（spec §14.17 第 5 条）。"""

    collection: CoinCollectionSeries
    member_series_ids: list[int]
    min_year: int | None
    max_year: int | None
    years: list[int]
    owned_years: list[int]
    missing_years: list[int]
    upgrade_years: list[int]
    year_specs: dict[int, list[CoinYearSpec]]
    size_mm: float | None
    size_band: str | None

    def year_specs_dict(self) -> dict[str, list[dict]]:
        return {str(year): [spec.as_dict() for spec in specs] for year, specs in self.year_specs.items()}


@dataclass(frozen=True)
class CoinTodayLot:
    """当日截拍专场里的一条拍品（推荐匹配用）。"""

    id: int
    session_id: int
    title: str
    start_price: float | None
    current_price: float | None
    image_urls: list[str]
    shop_name: str | None
    session_title: str
    auction_at: str | None
    shop_followed: bool = False


@dataclass
class CoinSearchLot:
    """全局搜索命中的一条拍品：拍品 + 所属拍场 + 个性化标签。"""

    id: int
    session_id: int
    lot_no: str | None
    title: str
    start_price: float | None
    current_price: float | None
    image_urls: list[str]
    tags: dict[str, str]
    session_title: str
    session_status: str | None
    auction_at: str | None
    auction_at_raw: str | None
    shop_name: str | None
    shop_followed: bool
    is_recommended: bool = False
    match_label: str = ""  # precise | similar


@dataclass
class CoinRecommendation:
    """一条目标购买推荐（P1 缺失年份 / P2 差品升级 / P4 年份匹配 / P5 面值探索）。"""

    priority: int
    collection: CoinCollectionSeries
    year: int
    year_labels: list[str]
    keyword: str
    score: float
    reason: str
    spec: CoinYearSpec | None = None
    # 相似推荐卡最多展示几条拍品：第一轮 1 条，第二轮兜底卡按剩余名额多条
    lot_limit: int = 1
    matched_lots: list[CoinTodayLot] = field(default_factory=list)

    @property
    def year_label(self) -> str:
        return self.year_labels[0] if self.year_labels else str(self.year)

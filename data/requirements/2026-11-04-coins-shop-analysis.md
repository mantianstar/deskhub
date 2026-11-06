# 需求卡：铜币 · 店铺分析（/coins/shops）

来源：`data/handoffs/2026-11-03-shop-analysis.md` + `data/prototypes/shop-analysis.html`（设计合同）
本轮变更：详情卡「站内查看该店数据」→「前往今日拍场」，指向 `GET /coins/session/{session_id}`；当日无场次则不渲染链接，显示「今日无拍卖」。

## 1. 验收句

1. 访问 `/coins/shops`（无参数）返回 200，表头 11 列依次为：店铺 / 靠谱分 / 样本 / 成交率 / 真实竞价 / 出价人数（中位）/ 围观→出价 / 同标题价差 / 同图重挂 / 交付 / 风险标签。
2. 默认只统计 `COALESCE(coin_lots.is_graded,0)=0`；库里只有评级盒店铺时该页显示空态，不报错；`graded=1` 后该店重新出现。
3. 某店有效样本（成交+流拍）为 3 时，靠谱分显示 `—`（class `score-none`），风险标签含「样本不足」，且不出现「可放心跟拍」类结论。
4. 某店当天有 `status='running'` 的专场时，详情卡出现「前往今日拍场」链接，`href="/coins/session/{该场 id}"`；同一店铺当天只有 `closed` 场次或没有场次时，该位置只显示文本「今日无拍卖」，全页不存在指向 `/coins/session/` 的链接（以该店为准）。
5. 全库无任何 `bid_user_num` 值时，真实竞价与出价人数列显示「—」，但靠谱分仍按成交率/交付等可得维度归一后给出数值，不因缺出价数据被扣分。
6. `/coins/analysis` 的「店铺画像」表保留原 7 列，店铺名变为链接，点击可到 `/coins/shops#shop-{id}`。

## 2. 待确认

| # | 问题 | 选项 | 推荐 |
|---|---|---|---|
| 1 | 当天多场时选哪场当「今日拍场」 | A. `status='running'` 优先，其次 `auction_at` 离现在最近（未开拍取最近将来，否则取最近结束）；B. 固定取 `auction_at` 最新一场；C. 全部列出 | A |
| 2 | 原型里的「导出 CSV」「只看已关注」「二级分类」是否本轮实现 | A. 二级分类+已关注保留，CSV 不做；B. 三者都保留；C. 只保留时间窗+裸币 | A |
| 3 | 旧表跳转目标 | A. `/coins/shops#shop-{id}`；B. `/coins/analysis?shop={id}`（现行为）；C. 不加链接 | A |

## 3. 改动链

| 文件 | 函数 / 模板 / 测试 | 动作 |
|---|---|---|
| app/repository.py | `list_coin_shop_analysis_rows`（新增，接在 1765 行 `list_coin_analysis_shops` 后） | 拍品级明细 + 场次 + 图片 key |
| app/repository.py | `list_coin_shop_today_sessions`（新增） | 今日场次，复用 456 行 `local_day_bounds` |
| app/coins/shops.py | 新建：`build_shop_report` / `_metrics` / `_score` / 工具函数 | 指标与五维分 |
| app/routers/coins.py | `shops_page`（新增，接在 1302 行 `analysis_page` 后） | `/coins/shops` |
| app/templates/coins/shop_analysis.html | 新建（`{% extends "base.html" %}`） | 榜单 + 详情 |
| app/templates/coins/_macros.html | `coin_nav` 的 items（1–15 行）加 `("shops","/coins/shops","店铺")` | 导航 |
| app/templates/coins/analysis.html | 168–170 行店铺名加 `<a href="/coins/shops#shop-{{ row.shop_id }}">` | 旧表跳转 |
| app/static/style.css | 末尾追加原型 93–147 行 `.coin-*` 组件块（`:root` 与预览副本不搬） | 样式 |
| tests/test_coins_pages.py | 新增 6 个用例（见 §7） | 冒烟 |

## 4. 数据层

```python
def list_coin_shop_analysis_rows(*, days: int | None = None,
                                  category_code: str | None = None,
                                  only_ungraded: bool = True) -> list[sqlite3.Row]
```
- 粒度：一拍品一行（`GROUP BY l.id`），供 Python 端聚合。
- 取列：`l.id, l.title, l.start_price, l.final_price, l.outcome, l.closed_at, l.is_graded, l.bid_num, l.bid_user_num, l.bid_users, l.bid_source`；`s.id AS session_id, s.shop_id, s.status, s.kind, s.auction_at, s.view_count, s.viewer_count, s.commission_rate, s.express_fee, s.express_day, s.is_storage`；`sh.name AS shop_name, COALESCE(sh.followed,0) AS followed`；`GROUP_CONCAT(DISTINCT img.oss_key) AS oss_keys`（LEFT JOIN `coin_lot_images`，184–192 行）。
- 条件：`substr(l.category_code,1,3)='110'`；`only_ungraded` → `COALESCE(l.is_graded,0)=0`；`days` → `COALESCE(s.auction_at, l.fetched_at) >= ?`（与 1740 行一致）；无店铺过滤，平台同标题基准即由本结果集算。
- 索引：走 `coin_sessions(shop_id)` 需确认存在（现有仅 `idx_coin_sessions_status`），单表量级可先不加。

```python
def list_coin_shop_today_sessions(shop_ids: list[int], tz: ZoneInfo,
                                  *, now: datetime | None = None) -> dict[int, list[sqlite3.Row]]
```
- `start, end = local_day_bounds(tz, now=now)`（456 行）；时间字段用 `coin_sessions.auction_at`（117 行，与 1074 行 `list_coin_today_lots` 同口径）。
- `WHERE s.shop_id IN (...) AND s.auction_at >= ? AND s.auction_at < ? ORDER BY s.auction_at, s.id`；取 `id, shop_id, status, kind, auction_at`。`shop_ids` 为空直接返回 `{}`（避免 `IN ()`）。

## 5. 计算层（app/coins/shops.py）

```python
MIN_SAMPLES = analysis.MIN_SAMPLES          # 5，复用
WEIGHTS = {"trust": 30, "heat": 20, "price": 20, "delivery": 20, "sample": 10}

def build_shop_report(rows, today_sessions: dict[int, list], *, now) -> list[dict]
def _metrics(group: list) -> dict
def _score(m: dict) -> tuple[int | None, dict]
def _pick_today_session(items: list, now) -> sqlite3.Row | None   # 待确认 1 的 A 方案
def _median(values), _pct(n, d), _norm_title(t), _clamp(x)
```

| 指标 | 计算式 |
|---|---|
| 有效样本 n | `sold + unsold`（`outcome='sold'` 且 `final_price` 非空 / `outcome='unsold'`），在拍不计 |
| 成交率 | `sold / n` |
| 真实竞价占比 | `#{sold 且 bid_user_num>=2} / #{sold 且 bid_user_num 非空}` |
| 覆盖率 | `#{bid_user_num 非空} / n`，页面小字「覆盖 x%」 |
| 出价人数（中位） | `median(bid_user_num)`，全空为 None |
| 围观→出价 | `Σ bid_user_num / Σ viewer_count`，仅 `status='closed'` 场次；任一为空则 None |
| 同标题价差 | 每个 `_norm_title` 平台成交中位数（样本 ≥5）作基准；`gap_title = 本店该标题成交中位 / 平台中位 - 1`；店铺取 `median(gap_title)`；无基准为 None |
| 同图重挂 | 流拍拍品的 oss_key 在该店后续（`auction_at` 更大）拍品出现即计 1 次；oss_key 全库出现 ≥10 次或跨 ≥5 店视为模板图剔除 |
| 交付 | 取该店 `auction_at` 最新场次的 `commission_rate / express_fee / express_day / is_storage` |
| 疑似托价（参考） | 对 `bid_users` 逐 uid 统计：本店出价场次 ≥10 且中签率 ≤10% → 问题 uid；其出价次数占本店出价 ≥20% 打标签；覆盖率 `cov<30%` 时只展示不计分 |

五维（0–100，None = 不计分）：

- 成交真实度：`0.6*rate_s + 0.4*realbid_s`；`rate_s = clamp(100*(rate-0.4)/0.5)`；`realbid_s = clamp(40+(rb-0.5)*100)`；`rb` 为 None 时只取 `rate_s`（权重归一）。
- 竞争热度：`bid_user_median` 分档 1→40 / 2→60 / 3→72 / 4→85 / ≥5→95；`围观→出价` 分档 <0.2%→40 / 0.3%→55 / 0.5%→70 / ≥0.8%→90，中间线性；两者取可用项平均，都缺为 None。
- 价格合理：`clamp(100 - 150*abs(gap))`，`gap` None 或基准样本 <5 为 None（不扣分）。
- 交付条件：佣金 ≤3%→100 / 3–5%→80 / >5%→60；运费 0→100、≤10→90、≤15→80、≤20→70、其余 55；发货 ≤2 天→100、3–4→85、≥5→65；可寄存 +5（封顶 100）；已得项平均。
- 样本可信：`n>=30→100`；`5<=n<30→20+80*(n-5)/25`；`n<5→None`。
- 总分：`Σ(w*s)/Σ(w of s≠None)` 四舍五入；`样本可信 is None` → 总分 None（渲染 `score-none` + 「样本不足」）。
- 风险标签：`同图重挂 ≥3` 红 / `出价人集中` 红（单一 uid 占成交 ≥60% 且 n≥10）/ `疑似托价（参考）` 黄 / `热度覆盖不足`（cov<30%）黄 / `样本不足` 黄 / 交付友好（佣金≤3% 且可寄存）绿。

## 6. 路由层

```python
@router.get("/coins/shops")
def shops_page(request, days: str = "", category: str = "",
               graded: str = "0", sort: str = "score", follow: str = "0") -> Response
```
- `days`：`""|30|90|180`，非法按 `""`（同 1264–1267 行 try 写法）。
- `category`：只接受 `repository.list_coin_categories("11000")` 里的 code，否则 `""`。
- `graded`：`"1"` 才含评级盒，默认 `"0"`。
- `sort`：`score|rate|gap|lots`，非法回落 `score`；`None` 分一律排末尾。
- `follow`：`"1"` 只看 `sh.followed=1`（待确认 2）。
- 流程：`rows = repository.list_coin_shop_analysis_rows(...)` → `shops = shops_module.build_shop_report(rows, today_sessions, now=datetime.now(cfg.app.tz))` → `repository.list_coin_shop_today_sessions([...])`。
- context：`shops / kpis(覆盖店铺数、有效样本、样本充足店铺数、待核查店铺数) / categories / days / category / graded(bool) / sort / follow / min_samples / total_count`。

## 7. 模板层

`app/templates/coins/shop_analysis.html`：

```
{% extends "base.html" %}
{% block title %}店铺分析 · deskhub{% endblock %}
{% block content_class %} content-wide{% endblock %}
{% block content %}{{ coin_nav("shops") }} → coin_head → 筛选 form → .coin-kpis ×4
  → 榜单 .table-wrap > table.sources-table.coin-analysis-table.coin-shop-table
     11 列表头照原型 224–236 行；每店两行：`tr.coin-shop-row[data-target=shop-{id}]` + `tr.coin-shop-detail#shop-{id}`
  → 详情三栏 grid：五维评分（progress 条）/ 最近成交样本 5 条 / 热度栏（场次围观人数、围观次数、出价次数中位、出价人数中位）
  → 详情第三栏买家结论：关注状态、今日拍场位、复制按钮、结论句
  → <details class="coin-notes"> 口径说明（原型 379–395 行，第 392 条改文案）
{% endblock %}
{% block scripts %} 行展开 JS（原型 398–427 行，含 Enter/Space）{% endblock %}
```

今日拍场位（替换原型 294 行）：

```jinja
{% if shop.today_session %}
  <p><a class="coin-detail-link" href="/coins/session/{{ shop.today_session.id }}">前往今日拍场</a></p>
{% else %}
  <p class="coin-detail-now"><span class="coin-fact-label">今日拍场</span>今日无拍卖</p>
{% endif %}
```

CSS：原型 93–147 行整段并入 `app/static/style.css` 尾部；`:root` 与 48–88 行预览副本删除（`.score*` 已在 296–312 行，`.coin-kpis` 在 1220 行）。

## 8. 测试清单（tests/test_coins_pages.py）

1. `test_shops_page_lists_11_columns`：造 1 店 6 条成交/流拍 → 200，11 个表头文案全在。
2. `test_shops_page_today_link_points_to_running_session`：当天 running 场次 → 详情含 `/coins/session/{id}`。
3. `test_shops_page_shows_no_auction_today`：该店只有昨天场次 → 含「今日无拍卖」，且不含 `/coins/session/`。
4. `test_shops_page_prefers_running_when_two_sessions`：同一天 closed + running → 链接指向 running（待确认 1 定为 A 时）。
5. `test_shops_page_filters_graded_and_days`：默认不统计评级盒；`graded=1` 出现。
6. `test_shops_page_insufficient_sample_and_missing_bid`：2 条样本 + 无 bid 数据 → `score-none`、「样本不足」、真实竞价 `—`，靠谱分仍渲染。
7. `test_analysis_shop_table_links_to_shops`：`/coins/analysis` 店铺名含 `/coins/shops#shop-`。

## 9. 明确不做

- 不改 `/coins/analysis` 的筛选与统计行为（只加链接）；不改 `build_report` 任何输出。
- 不做导出 CSV、不分页、不做店铺外链、不展示出价人明细（uid 只参与计数与脱敏标签）。
- 不引入前端的库/框架，行展开用原生 JS（同原型）。
- 不做起拍溢价、Numista 偏离、标题重复率（交接单已否决）。
- 不新增数据库表/迁移；不改采集链路与 `bid_source` 写入。
- 不改既有测试断言与既有页面文案。

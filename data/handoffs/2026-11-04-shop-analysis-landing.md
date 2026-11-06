status: done

# 铜币「店铺分析」页落地（续跑）

上一份交接：`data/handoffs/2026-11-03-shop-analysis.md`（方案与口径，仍有效；本文件只记落地进度）。

## 已完成（本轮已落盘）
- 设计合同重读完毕：`data/prototypes/shop-analysis.html`（11 列榜单 / 五维 / 详情三栏 / 口径说明）。
- `app/repository.py` 新增两个函数（紧邻 `list_coin_analysis_shops` 之后）：
  - `list_coin_shop_analysis_rows(*, level1="110", category_code=None, only_ungraded=True, days=None)`：拍品级明细，带店铺 / 场次热度 / 交付字段 / `image_keys`（GROUP_CONCAT）。
  - `list_coin_shop_today_sessions(shop_ids, tz, *, now=None)`：按店铺取当日场次，口径 `local_day_bounds` + `coin_sessions.auction_at`。
- 新建 `app/coins/shops.py`：`build_shop_report(rows, today_sessions, *, min_samples)` → `ShopReport(shops, totals)`；含 11 列指标、五维评分（30/20/20/20/10）、风险标签、详情（rating / facts / 最近成交 / 热度 / verdict / today_session）。已补 `rate` / `gap` / `lots` 原始值供排序。
- `app/routers/coins.py`：import `coins_shops`；新增 `_SHOP_SORTS`、`_sort_shops`、`GET /coins/shops`（参数 days / category / graded / follow / sort）。
- `app/templates/coins/_macros.html`：导航新增 `("shops", "/coins/shops", "店铺")`。

## 未完成
1. `app/templates/coins/analysis.html` 店名加链接（目标 `/coins/shops#shop-{id}`）。注意该文件有 3 处 `        <td>{{ row.name }}</td>`，直接替换会报歧义；定位第 170 行（店铺画像表，紧邻 `{{ row.lots }}`）后用更大上下文替换。
2. 新建 `app/templates/coins/shop_analysis.html`：`extends base.html`、`content_class` 为 ` content-wide`、`coin_nav("shops")`、`coin_head`、筛选 form（时间窗 / 二级分类 / 裸币 / 只看已关注 / 排序）、4 个 KPI、11 列表头、`tr.coin-shop-row[data-target=shop-{id}]` + `tr.coin-shop-detail#shop-{id} hidden`、五维 progress、最近成交、热度、`<details class="coin-notes">` 口径说明、`{% block scripts %}` 原生行展开 JS（含 hash `#shop-{id}` 自动展开）。
   - 「前往今日拍场」块（替换原型 294 行）：
     ```jinja
     {% if shop.today_session %}
     <p><a class="coin-detail-link" href="/coins/session/{{ shop.today_session.id }}">前往今日拍场</a></p>
     {% else %}
     <p class="coin-detail-note">今日无拍卖</p>
     {% endif %}
     ```
3. `app/static/style.css` 末尾并入原型 93–147 行组件样式（跳过 `.coin-proto-note`；`.score` / `.coin-analysis-table` / `.coin-kpis` / `.content-wide` 已存在，不要重复定义）。
4. `tests/test_coins_pages.py` 新增用例：
   - `/coins/shops` 200 + 11 列表头；
   - 今日 running 场次 → 详情含 `href="/coins/session/{id}"` 与「前往今日拍场」；
   - 只有昨日场次 → 含「今日无拍卖」且不含 `/coins/session/`；
   - 同一天 running + closed 多场 → 选 running；
   - 裸币过滤（`is_graded=1` 的店默认不出现，`graded=1` 后出现）；
   - 有效样本 3 → 靠谱分为「—」+「样本不足」；
   - 无 bid 数据仍出分（不扣分）；
   - analysis 页店名带 `/coins/shops#shop-` 链接。
   - 造数注意：`PRAGMA foreign_keys = ON`，`coin_lots.category_code` 必须先 `repository.upsert_coin_categories([("11050","中国铜元",1,"11000",2,None,None,None)])`；今日场次用测试内 `_today_auction_at()`；`bid_users` 只能经 `update_coin_lot_filter_heat` 写入，托价/集中度用例可不造。
5. `CHANGELOG.md` 顶部新增 v1.118 条目（起因 → 改动 → 验证）；如 plan/spec 有铜币分析章节，补 `/coins/shops` 一行当前状态。

## 下一步
1. 按上面 1→5 顺序补齐；模板与 CSS 参考原型 151–390 行，不新增依赖、不动 analysis 统计行为。
2. 验证：`.venv/bin/python -m pytest tests/test_coins_pages.py -q`，再跑全量 `pytest -q` 收尾。
3. 全绿后把本文件与 `2026-11-03-shop-analysis.md` 首行改成 `status: done`。

## 约束 / 已拍定的取舍
- 今日口径：`repository.local_day_bounds(tz)` + `coin_sessions.auction_at`（北京时间存储串）。
- 当天多场：`status='running'` 优先，其次 `auction_at` 最晚（`id` 兜底）；落地在 `shops._pick_today_session`。
- 筛选：做时间窗 / 二级分类 / 仅裸币 / 只看已关注 / 排序；**导出 CSV 不做**（原型只是示意按钮）。
- 旧「店铺画像」表保留，店名跳 `/coins/shops#shop-{id}`；不改表头与既有文案。
- 出价人明细不展示；bid 覆盖率约 1/21，缺失按可得权重归一不扣分；样本 <5 不给总分。
- 用户否决项不变：起拍溢价、Numista 估值偏离、标题重复率、店铺外链。

## 读取起点
- `app/coins/shops.py`（已写全，先读它对齐字段名）
- `app/routers/coins.py#L1304-L1381`（排序 + 路由）
- `app/repository.py#L1778-L1854`（两个新查询）
- `data/prototypes/shop-analysis.html#L151-L390`（模板与文案）
- `app/templates/coins/analysis.html#L152-L186`（店名链接目标处）
- `tests/test_coins_pages.py#L37-L129`（fixture 与造数写法）

status: done
# 2026-11-14 好品计数 / lot_no / 拍场好品区 / 全专场搜索

## 用户原句（不改写）
1、今日拍品页里好品推荐数反映到今日拍场里，增加好品 X，跟在精准 X后面，圆圈里的数字要加上好品推荐数
2、今日拍品页3种推荐卡片里要加上lot_no，在拍品名称前
3、拍场详情页也要增加 好品推荐区域卡片，放在精准推荐后面
4、拍品详情页搜索现在只搜索当前分页的数据，改成搜索整个专场的数据

需求卡：data/requirements/2026-11-14-today-best-lotno-session-search.md（planner 已出）。
类型 L：已写任务卡；剩余 编辑 → 验证 → CHANGELOG。

## 已确认的定位（读过的行号）
- 今日推荐页 `/coins/collection`：`app/routers/coins.py#collection_page`（2059 起）；模板 `app/templates/coins/collection.html`。
  - 3 类卡片：`render_rec_card(item, kind)`（collection.html:25-66，kind=precise/similar 共用）、`render_best_card(lot)`（68-90）。
  - 拍品名称行：collection.html:49（`<span class="coin-rec-lot-title">{{ lot.title }}</span>`）、:73（`<p class="coin-rec-meta">{{ lot.title }}</p>`）、:84（best 卡 lot 行的 `coin-rec-lot-title`）。
  - 「今日拍场」rail：212-243；每场 meta 在 :227 `{{ session.lot_count }} 件 · 精准 {{ session.hit_count }} · 相似 {{ session.similar_count }}`；圆圈在 :231 `{{ session.hit_count + session.similar_count }} 件`。
- collection_page 数据段：`best_items = coins_best.load_best_lots(cfg, series=series)` 在 coins.py:2106；today_sessions 聚合在 2217-2264；排序 2266-2282；rail 过滤 2284-2288；other_hit_total 2296-2298。
- 模型：`app/models.py` `CoinTodayLot`(1292，无 lot_no)、`CoinBestLot`(1308，无 lot_no，frozen，字段 shop_followed 有默认值)。
- repository：`_list_coin_lots_between`(app/repository.py:1439，SQL 1447-1455，构造 1468-1482)；`list_best_grade_lots`(1644，SQL 1653-1667，构造 1681-1701，SQL 里 `l.*` 逐个列出)。`save_coin_lot_grade(lot_id, parsed, model_raw=, model=)` 在 1600，parsed 用 `grade_interval/grade_score/color_grade` 等键，只 UPDATE 已有行；行由 `insert_pending_coin_lot_grades`(1488) / `best.match_today_lots` 创建。
- 拍场详情页 `/coins/session/{id}`：`session_page`（coins.py:284-351）；模板 `app/templates/coins/session.html`。
  - `lot_card(lot, reason=None, index=1)` 宏 65-118；精准 section 120-127；相似 129-136；搜索结果占位 138-142；其他拍品分页 144-166；JS 168-216（`cards = document.querySelectorAll('.coin-lot[data-search]')` 在 184）。
  - `COIN_OTHER_LOT_PAGE_SIZE = 20`（coins.py:1654）；分页结果变量 `paged_other, other_page, other_pages`（321-323），上下文在 338-350。
- 好品：`app/coins/best.py#load_best_lots(config, *, series="")`（219），取当日 11070 类目、grade_score>=BEST_GRADE_THRESHOLD 的拍品，返回 `CoinBestLot`（含 session_id）。
- 测试：`tests/test_coins_pages.py`，`web` fixture 42-47（TestClient 真 lifespan，临时 DB），`_today_auction_at` 50-56，`_seed_session` 59 起（造店铺+专场+推荐拍品，lot_no="Lot.1"，标题「东印度公司1808年硬币」）；现有搜索测试 537-549（不要改断言）。

## 已定方案（直接执行）
1. 今日拍场好品计数（collection_page + collection.html）
   - 用已加载的 `best_items` 按 session_id 聚合：`best_by_session`（按 lot.id 去重）。today_sessions 的初始 dict 增加 `"best_count": 0`，聚合完 similar 后写回。
   - rail 过滤（2284-2288）加 `or session["best_count"]`，让只有好品的场次也进「今日拍场」。
   - 排序 key（2266-2282）在 similar 后加一档 `2 if session["best_count"]`，followed 顺延；tie-break 加 `-session["best_count"]`。
   - 模板 :227 改成 `{{ session.lot_count }} 件 · 精准 {{ session.hit_count }} · 好品 {{ session.best_count }} · 相似 {{ session.similar_count }}`（好品紧跟精准）；:231 圆圈改 `session.hit_count + session.best_count + session.similar_count`。
   - summary 区块 216-219 不动（未要求）。
   - 去重口径：好品不参与 precise/similar 的互斥，简单相加（好品是 11070 外币，口径独立）。
2. 三卡 lot_no
   - `CoinTodayLot` 末尾加 `lot_no: str | None = None`；`CoinBestLot` 末尾（shop_followed 后）加 `lot_no: str | None = None`。
   - `_list_coin_lots_between` SQL 加 `l.lot_no`，构造加 `lot_no=row["lot_no"]`；`list_best_grade_lots` 同样。
   - collection.html :49/:73/:84 名称前加 `{% if lot.lot_no %}{{ lot.lot_no }} {% endif %}`。
3. 拍场详情页好品区
   - session_page 里 `cfg = config.get()`，`best_lots = [lot for lot in coins_best.load_best_lots(cfg) if lot.session_id == session_id]`，传入上下文。
   - session.html 在精准 section（127 行 `</section>`）后、相似 section 前插：
     `<section id="coin-best-section">` + 标题「好品推荐 N 件」+ 新宏 `best_lot_card(lot)`（CoinBestLot 无 lot_id/outcome/median 等，不能复用 lot_card）；
     宏内容仿 best 卡：`section.coin-lot`（`data-search="{{ lot.lot_no or '' }} {{ lot.title or '' }}"`，id 用 `coin-best-{{ lot.id }}` 避免和其他 lot_card 的 `coin-lot-{{ lot.lot_id }}` 撞）、标题 `lot_no + title`、一行「好品推荐 · 评级 {{ lot.grade_interval or '—' }}」+ 铜色、起价/当前价、图片（lot.image_urls[:3]，class coin-lot-img coin-zoom，loading=lazy）。
   - JS recSections（173-175）加 `'coin-best-section'`。
4. 搜索覆盖整个专场（前端 pool，已选方案 C）
   - session_page：`page_start = (other_page - 1) * COIN_OTHER_LOT_PAGE_SIZE`；`search_lots = [(lot, idx + 1) for idx, lot in enumerate(other_lots) if not (page_start <= idx < page_start + len(paged_other))]`（全局 index 供 lot_card 的 `Lot.N` 兜底），上下文加 `"search_lots": search_lots`。
   - session.html 在其他拍品 section 后加 `<template id="coin-search-pool">{% for lot, index in search_lots %}{{ lot_card(lot, None, index) }}{% endfor %}</template>`。
   - JS：`const pool = document.getElementById('coin-search-pool');` 并在取到 cards 后 `if (pool) cards.push.apply(cards, Array.from(pool.content.querySelectorAll('.coin-lot[data-search]')));`（template 惰性，不加载图片）。

## 测试（加到 tests/test_coins_pages.py，复用 web/_seed_session/_today_auction_at）
- 造好品：`CoinLot(..., category_code="11070")` + `repository.insert_pending_coin_lot_grades([(lot_id, series_id?)])` 需要 series；更简单路径：直接 `best.match_today_lots(config.get())` 再 `repository.save_coin_lot_grade(lot_id, {"grade_interval": "XF40", "grade_score": 40, "color_grade": "红铜"}, model_raw="", model="test")`（需先 upsert CoinSeriesCn + 跑 match；参考 tests/test_coins_best.py:126-206 的 _session/_lot/_series 写法，注意那边 fixture 是 temp_db 不是 web，web 下可用 config.get()）。
- 断言：
  1. `/coins/collection` 页面含「好品 1」且圆圈为三数之和（注意 hit=1 默认、rail 最多 6 场）。
  2. session 页含 `id="coin-best-section"`、「好品推荐 1 件」、拍品标题。
  3. 造 21 条其他拍品，GET 第 1 页：`id="coin-search-pool"` 出现，且第 2 页拍品标题出现在 pool 内（用 `page.text.split('coin-search-pool', 1)[1]` 判断）。
  4. 推荐卡 lot_no：有 lot_no 的拍品，`Lot.123 标题` 连在一起；无 lot_no 不出现多余空格。
- 现有测试断言不改。

## 收尾
- 跑 `.venv/bin/python -m pytest tests/test_coins_pages.py -q`，最后全量 `pytest -q` 一次。
- CHANGELOG.md 最上方加 v1.159（2026-11-14）一条：起因/改了什么/验证结论/影响文件。
- 回复末尾按项目规则出「回执：」四行（`utils/token_cost/token_cost.py --round`）。

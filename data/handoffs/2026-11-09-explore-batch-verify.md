# 2026-11-09 页面探索真实联调验收 + 国家/面值修复（续跑）

status: done

## 已完成

- 真实环境联调（CDP 9222 在跑、8765 服务在跑）：
  - 53 条路径：`issuer='' st=1 a=1900-2026 m=3 t=28-33` → `total=53`，`failed/over_limit`，文案「当前探索结果超过 50 条，请重新筛选条件」，零写入。✅
  - ≤50 路径：`issuer=香港` 实为 56 条（也被拦，符合预期）；`issuer=澳门 st=1` → `total=24 / saved=24 / percent=100`，`?batch=20261109185823822393` 服务端渲染出中文（澳门 / 铜 / 黄铜 / 白铜 / 镍）。✅
- 联调暴露两处展示缺陷，已修（`pytest tests/test_coins_explore_page.py tests/test_coins_numista_web.py tests/test_coins_pages.py -q` → 131 passed）：
  1. 国家列被页面分组头覆盖成货币名（`Pataca (1894-date)`，中文未译）：`explore_page` 新增 `_SLUG_TO_ZH` / `_SLUG_TO_EN`，`start()` 把解析后的 slug 传给 `_run(issuer=…)`，`_row(issuer_slug=…)` 命中时用筛选国家回填中英文。
  2. 标题既无「 - 」也无「 (」时面值为空（如 `5 Avos`）：`numista_web.parse_catalogue_index` 兜底整串作面值（逻辑与注释一致，原代码即如此断句）。
- 旧交接的两项已确认无需再处理：CHANGELOG 已记到 v1.148；`start=0/1` 共用端点已记入 v1.146，未收到反对。

## 未完成

（无）

## 收尾（2026-11-09 续跑）

1. 用户已重启 8765；重跑「澳门」批次 `20261109190708124477`：`total=24 / saved=0 / skipped=24`（24 条全命中老行），`?batch=20261109190708124477` 仍渲染出结果，采样国家列=澳门、面值=50 Avos / 10 Avos 非空。✅
2. 用例已补：`tests/test_coins_explore_page.py` 国家回填（`issuer=macao` + 分组头只剩 `Pataca (1894-date)`）、`tests/test_coins_numista_web.py` 无括号标题 `5 Avos` 面值；同批重跑可见已有 `test_run_page_explore_reskipped_ids_visible_in_new_batch` 覆盖。`pytest tests/test_coins_explore_page.py tests/test_coins_numista_web.py tests/test_coins_pages.py -q` → 133 passed。
3. `batch_id` 口径已按用户拍板实现：不改行上 `batch_id`，新增 `coin_page_explore_batch_items` 成员表，查询按本批 ID 集合过滤。
4. CHANGELOG 已记 v1.149。

## 下一步

无。

## 约束

- 取 Numista 页面只能 `connect_over_cdp`，普通 HTTP 403。
- SQL 只写 `app/repository.py`，翻译映射不散落路由。
- 不改既有文案与测试断言，不做无关重构。

## 读取起点

- `app/coins/explore_page.py`：`_SLUG_TO_ZH`(59)、`start`(151)、`_run`(193)、`_row`(270)
- `app/coins/numista_web.py`：`parse_catalogue_index`(260-300)
- `app/repository.py`：`upsert_coin_page_explore_item`(~1287)
- `tests/test_coins_explore_page.py`、`tests/test_coins_numista_web.py`

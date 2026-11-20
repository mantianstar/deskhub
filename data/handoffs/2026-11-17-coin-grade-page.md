status: done
# 交接：新增铜币品相管理页面

## 已完成

- 「我的资产」子导航（`coin_collection_tabs`）在观察系列之后新增「铜币品相管理」页签，`coin_nav` 映射到 assets。
- 路由：`GET /coins/lot-grades`（[app/routers/coins.py](app/routers/coins.py) `lot_grades_page`）——五条件查询（拍卖日期 / 国家 / 面值 / 系列号手填 / 标题模糊）+ 分页（20/页）。
- 取数：`repository.list_coin_lot_grades`（[app/repository.py](app/repository.py)）取 `coin_lot_grades` **全部状态**，join 拍品 / 拍场 / `coin_series_cn`，年份取 `coin_lots.tags['发行年份']`，系列号取 `COALESCE(cs.numista_id, l.series_numista_id)`。
- 模板：[app/templates/coins/lot_grades.html](app/templates/coins/lot_grades.html)，拍品前两张图并排居左（`coin-zoom` 复用 base.html 灯箱），右侧 3 行（名称 / 拍场·日期 / 系列号·国家·年份·面值·品相），空值填 `-`。
- 名称链到 `/coins/session/{session_id}#coin-lot-{lot_id}`；【⋯ 更多】→【评级 / 重新评级】。
- 评级：`POST /coins/lot-grades/{lot_id}/grade` 异步（`asyncio.to_thread` + 后台 task 集合），`best.start_grading_lot` 在跑集合拦同一拍品重复提交，列表页显示「评级中…」；`best.grade_lot` 与批量评级共用抽取出的 `_grade_one`。
- 品相字段点击弹窗展示 `coin_lot_grades.model_raw`，可关闭（× / 遮罩 / Esc）。
- 无多选；SQL 只在 `repository.py`；`app/models.py` 新增 `CoinGradeLot`。
- 测试：`tests/test_coins_pages.py::test_lot_grades_page_tab_after_watchlist_and_empty_state`；`pytest -k "lot_grades or watchlist"` 3 passed。
- CHANGELOG v1.165 已记。

## 未完成

- 未跑全量 `pytest -q`（本段会话输入预算已到 1.2M 上限）。
- `spec/coins.md`、`plan/coins.md` 未原地补页面与接口说明。
- 未做浏览器视觉验收（卡片布局、弹窗、两点并排图在真实数据下的观感）。
- 未验证真实视觉模型下的异步评级端到端（测试配置无 `coin_grade.vision`，只覆盖到路由与空态）。
- 品相显示口径暂定 `grade_interval → grade_cn → '-'`，如需改为中文品相或净等级，改模板一行。

## 下一步

1. 新会话先 `pytest -q` 一次（全量）。
2. 用真实数据打开 `/coins/lot-grades` 检查卡片布局与图片灯箱；确认「评级」按钮链路。
3. 把页面与 `GET/POST /coins/lot-grades*` 补进 `spec/coins.md`、`plan/coins.md`。

## 约束

- 只做需求点名的六件事：不改既有文案与测试断言，不做无关重命名 / 重构。
- `Read` 带 `offset/limit`，单段 ≤120 行；SQL 只写 `app/repository.py`；无多选。

## 读取起点

- `app/routers/coins.py`（`lot_grades_page`、`grade_lot_page`，约 725–820 行）
- `app/templates/coins/lot_grades.html`
- `app/repository.py`（`list_coin_lot_grades`、`get_grade_lot`）
- `app/coins/best.py`（`_grade_one`、`grade_lot`、`start_grading_lot`）

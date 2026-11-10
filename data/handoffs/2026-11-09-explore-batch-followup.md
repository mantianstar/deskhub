# 2026-11-09 采集中心「批量方式(页面)」续跑：bug 已修 + 两处新需求

status: superseded

> 已被 [2026-11-09-explore-batch-verify.md](2026-11-09-explore-batch-verify.md) 取代：联调已完成，`batch_id` 口径已拍板并落地，当前口径以 verify 为准。

## 已完成

- 根因定位（bug-reproducer）：`app/templates/coins/ingest.html` 内联 script 早于 `#page-explore-progress` 渲染，点【获取列表】时 `progress === null` 抛 TypeError，`:136` 之后 `send('1')` 从未执行 → 后端无任务、无进度、`coin_page_explore_items` 零行；后端链路本身正常（手工 POST 可启动，无 CDP 时 30s 内 failed/fetch_failed）。
- 已修复：script 整体移到「探索结果」面板头之后（现 L96-173，span 在 L94）；删除按钮文案「采集中」及两处还原，按钮文字恒为「获取列表」只用 disabled；进度只写标题最右 span，结束后回填「多选后一次加入队列」。
- 新增用例 `tests/test_coins_pages.py::test_ingest_page_page_explore_progress_after_script_element`（断言 span 位置早于脚本取值、页面不含「采集中」）。
- `pytest tests/test_coins_pages.py tests/test_coins_explore_page.py -q` → 116 passed。
- 2026-11-09 续跑：短词翻译改走大模型 —— `translate_llm.short_texts_zh`（一次批量、等长数组、失败抛 `TranslationError`）+ `explore_page._texts_zh`（去重、进程内 `_TEXT_CACHE`、词表命中优先、无 Key/失败回退词表），覆盖名称 / 国家 / 执政者 / 面值 / 材质。
- 面值兜底：`numista_web.parse_catalogue_index` 标题无「 - 」时取第一个「 (」前作面值、执政者留空。
- 新增用例：`test_coins_numista_web.py::test_parse_catalogue_index_denomination_without_ruler`、`test_coins_explore_page.py::test_run_page_explore_translates_short_texts_in_one_batch`；`pytest tests -q` → 590 passed, 1 skipped。

## 未完成

（无，以下两项均已闭环）

1. ~~真实环境联调~~：已由 verify 完成（53 条被拦、澳门 24 条入库并渲染中文）。
2. ~~`batch_id` 口径~~：用户已拍板「不改行上 `batch_id`，新增 `coin_page_explore_batch_items` 成员表，查询按本批 ID 集合过滤」，已落地（`app/models.py`，`SCHEMA_VERSION = 36`）。

## 下一步

1. 联调验收；若需要再讨论 `batch_id` 口径（未拍板前不动）。

## 约束

- 取 Numista 页面只能 `connect_over_cdp`，普通 HTTP 返回 403。
- SQL 只写在 `app/repository.py`，建表在 `app/models.py`；翻译映射不散落到路由。
- 探索数据只作展示、字段不完整；加入队列后仍走正常 4 步采集。
- 不改既有文案与测试断言，不做无关重构。

## 读取起点

- `app/coins/explore_page.py`：`_issuer_cn`(218)、`_row`(222)、`start`(148)、`_fail`(180)
- `app/coins/numista_web.py`：`CatalogueItem`(119)、`parse_catalogue_index`(255-306)、`fetch_catalogue_page`
- `app/coins/translate_llm.py`、`app/llm.py`、`app/coins/numista.py`（LLM 调用范例）
- `app/templates/coins/ingest.html`：L91-173（已修好的脚本）
- `tests/test_coins_pages.py:1231`、`tests/test_coins_explore_page.py`

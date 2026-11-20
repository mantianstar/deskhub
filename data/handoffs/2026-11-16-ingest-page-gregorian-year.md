status: done

# 采集中心：批量方式(页面) 增加「公历年份」

## 已完成
- 定位到改动点：`app/templates/coins/ingest.html`
  - 第 70 行：批量方式(页面)表单的「年份」字段
    `<label class="field">年份<input class="search-input" type="text" id="page-year" placeholder="如 1900-2026（可空）" autocomplete="off"></label>`
  - 第 112 行：提交时组装的 payload `year: document.getElementById('page-year').value,`
- 确认后端参数链路在 `app/coins/explore_page.py`：`catalogue_page_url(*, issuer, st, year, material, size)`（第 103 行）、`fetch/scan`（第 157/164 行）按 `year` 拼 Numista 列表页 URL。

## 未完成
- 未做任何代码改动（本轮仅定位，预算到顶）。

## 下一步
1. 在 ingest.html 第 70 行「年份」字段之后插入「公历年份」字段（id 建议 `page-gregorian-year`，文案按需求原句「公历年份」）。
2. 取值随 payload 提交（第 112 行附近），后端 `app/coins/explore_page.py` 的 `catalogue_page_url` / 调用方按需接参；需先确认 Numista 列表页 URL 是否支持公历年份参数，若仅作记录则只存值不参与 URL。
3. 同步 `tests/test_coins_explore_page.py`（页面表单字段）与 CHANGELOG 一条。

## 约束
- 只加字段，不改既有「年份」字段文案 / 校验 / 样式（RULES §七）。
- 需求原句：`采集中心- 探索条件 · 批量方式(页面)，增加公历年份条件，放到原年份字段后面`

## 读取起点
- `app/templates/coins/ingest.html`（第 24-115 行）
- `app/coins/explore_page.py`（第 85-170 行）
- `tests/test_coins_explore_page.py`
- `app/routers/coins.py`（ingest 路由）

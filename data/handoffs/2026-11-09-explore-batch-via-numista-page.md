# 2026-11-09 采集中心新增「探索条件 · 批量方式(页面)」（交接）

status: superseded

> 已被 [2026-11-09-explore-batch-verify.md](2026-11-09-explore-batch-verify.md) 取代：联调验收、CHANGELOG（v1.146 / v1.148 / v1.149）、`start` 端点口径均已闭环，当前口径以 verify 为准。

## 需求原句

【任务目标】
在现有的“采集中心”页面中，新增一个【探索条件 · 批量方式(页面)】区块，实现基于 Numista 网页的批量探索采集功能。

【详细需求】

1. 页面区块新增
在【探索条件 · ID 方式】下方新增区块【探索条件 · 批量方式(页面)】，包含：
- 第一行查询条件：
  - 国家：可输入可下拉（参考 `https://api.numista.com/v3/issuers` 接口）。
  - 货币类型：下拉菜单（`https://en.numista.com/catalogue/页面的coins` 列表）。
  - 年份：输入框。
  - 材质：下拉菜单（`https://en.numista.com/catalogue/页面的composition`  列表）。
  - 尺寸：输入框。
  - 【探索】按钮。
- 第二行：
  - 拼接生成的 Numista 查询链接（只读展示）。
  - 【获取列表】按钮。

2. 交互与逻辑
- 点击【探索】：前端读取表单选中的中文值，映射回英文/数字，拼接成 类似``https://en.numista.com/catalogue/index.php?r=&st=1&cat=y&im1=&im2=&ru=&ie=&no=&v=&cu=&a=1900-2026&dg=&i=&b=&m=3&f=&t=28-33&t2=&w=&mt=&u=`` 形式的链接，展示在第二行。可参考之前候选页页面采集里有部分参数
- 用户点击链接打开页面，人工确认页面数据无误后，点击【获取列表】。
- 点击【获取列表】：触发后端采集流程。此时，【探索结果】区块标题最右侧展示【正在采集：XX%】（动态更新），原【多选后一次加入队列】文案隐藏或替换。

3. 后端采集与入库（核心逻辑）
- 编写一个解析目标 Numista 页面 HTML 的方法，提取以下字段：series id（作为主键）、名称、国家、执政者、面值、年份、材质、尺寸。
- 数据入库：以 series id 为主键，遇到数据库中已存在的数据，直接跳过。
- 采集进度反馈：前端通过轮询（Polling）或 WebSocket 获取后端采集进度，实时更新【正在采集：XX %】。
- 风控要求：判断最终采集条数，若超过 50 条，立即停止采集，并向前端返回错误提示“当前探索结果超过 50 条，请重新筛选条件”。
- 注意：此次入库的数据仅作探索结果展示，字段不完整。用户将探索结果加入任务队列后，后续仍走正常的 4 步采集流程。

4. 数据翻译
- 国家、货币类型、材质：后端在获取数据后，需将英文值翻译成中文，并与原英文值一起入表。前端页面展示时， 只显示中文。

## 探查结果（2026-11-09，只读验证）

### 1. 查询条件与参数

| 查询项 | 页面参数 | 真实数据源 / 取值 |
|---|---|---|
| 国家 | `e` | 页面 `search_issuers.php?q=&p=` 返回 JSON（`id`/`text`/`level`/`section`）；API `GET /v3/issuers` 实测 12,028 条，字段 `code`/`name`/`flag`/`wikidata_id`/`level`/`parent` |
| 货币类型 | `cu` | `get_currencies.php?country={e}&nc=true` 返回 `<option value="数字ID">英文名</option>`；未选国家返回空 |
| 年份 | `a` | 输入框，支持单年 `1900` 或区间 `1900-2026` |
| 材质 | `m` | 页面静态 `<select name="m">`，约 70 项，如 `3`=Copper、`5`=Bronze、`1`=Silver、`2`=Copper-nickel |
| 尺寸 | `t` | 输入框，支持单值 `30`（±0.5mm 容差）或区间 `28-33`；`t2` 是 Width，不是尺寸 |
| Coins 子类 | `st` | `1`=标准流通币、`2`=流通纪念币、`3`=非流通币、`47`=收藏币、`154`=应急货币、`5`=地方货币、`6`=样币 |

真实选项样例：

```json
{"id":"china","text":"China","level":1,"section":1}
{"id":"chine","text":"China, People's Republic of","level":2,"section":0}
{"id":"france","text":"France","level":2,"section":0}
{"id":"royaume-uni","text":"United Kingdom","level":2,"section":0}
```

```text
get_currencies.php?country=chine&nc=true
<option value="9078">Foreign Exchange Certificates</option>
<option value="9235">Rénmínbì (1949-1955)</option>
<option value="198">Rénmínbì (1955-date)</option>
<option value="11192">Euro</option>
```

```text
材质常用映射：3=Copper 铜、5=Bronze 青铜、4=Brass 黄铜、2=Copper-nickel 白铜（铜镍）、
1=Silver 银、6=Gold 金、9=Steel 钢、15=Stainless steel 不锈钢、45=Aluminium 铝、
10=Aluminium bronze 铝青铜、8=Nickel 镍、16=Nickel brass 镍黄铜、11=Zinc 锌、13=Iron 铁、
bi=Bimetallic 双金属、tri=Trimetallic 三金属、p=Precious 贵金属、np=Non precious 非贵金属
```

### 2. 真实链接样例

实测可用，页面标题 `Standard circulation coins – Numista`，页面总数 **53**（正好触发 >50 风控）：

```text
https://en.numista.com/catalogue/index.php?r=&st=1&cat=y&im1=&im2=&ru=&ie=&no=&v=&cu=&a=1900-2026&dg=&i=&b=&m=3&f=&t=28-33&t2=&w=&mt=&u=
```

取页方式：直接 `curl` 返回 **403**（Cloudflare），`api.numista.com` 不带 Key 返回 **401**。必须复用 `app/coins/numista_web.py` 现有的 `connect_over_cdp` + 本地 Chrome（`http://127.0.0.1:9222`）。

### 3. 8 字段真实样例

| 字段 | 样例 A | 样例 B |
|---|---|---|
| series id | `10387` | `14275` |
| 名称 | `10 Pennia - Nicholas II` | `2 Paisa - Sayaji Rao III (Baroda)` |
| 国家 | `Finland` | `India › Indian Local and Princely states › Baroda, Princely state of` |
| 执政者 | `Nicholas II` | `Sayaji Rao III (Baroda)` |
| 面值 | `10 Pennia` | `2 Paisa` |
| 年份 | `1895-1917` | `1940-1950 (1883-1893)` |
| 材质 | `Copper`（3） | `Copper`（3） |
| 尺寸 | `30.0 mm` | `29.5 mm` |

原始列表项（现有 `parse_catalogue_index` 已能解析 7 项）：

```text
numista_id=10387
title='10 Pennia - Nicholas II'
issuer='Finland'
year_text='1895-1917'
composition='Copper'
weight_g=12.8
size_mm=30.0
catalog_no='KM# 14, Schön# 9, Kahnt/Schön# 22, N# 10387'
```

```text
numista_id=14275
title='2 Paisa - Sayaji Rao III (Baroda)'
issuer='India › Indian Local and Princely states › Baroda, Princely state of'
year_text='1940-1950 (1883-1893)'
composition='Copper'
weight_g=16.3
size_mm=29.5
catalog_no='Y# 32, N# 14275'
```

字段取法要点：
- 列表页没有独立「执政者 / 面值」字段，藏在 `title` 的 `"{面值} - {执政者}"` 里，按第一个 `" - "` 拆分。
- 无 `" - "` 的条目（如 `10 Penniä (Civil War Coinage)`）执政者留空，不能硬拆。
- 国家可能是多级路径，建议同时存原始串与末级名。
- 年份可能是双历法 `1940-1950 (1883-1893)`；现有解析取首尾数字会得到 `min=1940, max=1893`，新逻辑应只取第一个括号前的区间。
- 材质、国家、货币入库时需同时存英文原值与中文译名，前端只显示中文。

## 已确认口径（2026-11-09）

1. 「货币类型」用 `st`（Coins 子类：1 标准流通币 / 2 流通纪念币 / 3 非流通币 / 47 收藏币 / 154 应急货币 / 5 地方货币 / 6 样币），不用 `cu`（币种）。
2. 国家下拉：实际落地用静态词表 `ISSUER_CODES`（`app/coins/explore_page.py:116-118`），不接 `/v3/issuers` 全量（12,028 条）；曾计划用 `search_issuers.php?q=&p=` 常用子集，未采用。
3. >50 风控以页面 `results found` 页面总数为准，不按实际解析条数。
4. 探索结果区块展示也要中文（不只国家 / 货币 / 材质，该区块所有展示内容均中文化）。

## 已完成

- 只读探查完成：查询参数、选项数据源、真实链接、8 字段样例、取页风控链路已确认（详见上节）。
- 2026-11-09 实现完成（方案见 `2026-11-09-explore-batch-plan.md`）：
  - 新表 `coin_page_explore_items`（models v34→35 + migrations 登记），repository 新增 upsert（ON CONFLICT DO NOTHING）/ list。
  - 新模块 `app/coins/explore_page.py`：选项常量、国家解析（中文/英文/slug→slug）、后端重算 URL、>50 硬停、内存单飞任务、双语入库。
  - `numista_web.py`：`CatalogueItem` 增 ruler/denomination、年份只取首个括号前、`fetch_catalogue_page(url=...)`。
  - 路由：`POST /coins/ingest/page-explore`、`GET /coins/ingest/page-explore/status`、`GET /coins/ingest?batch=`。
  - 模板：ID 方式下方新区块 + 800ms 轮询，完成后跳 `?batch=` 由服务端渲染结果，复用既有勾选/入队。
  - 测试：`tests/test_coins_explore_page.py` 新增，另补两处；三文件 127 passed。

## 未完成

（无，以下三项均已闭环）

- ~~真实环境联调~~：已由 verify 完成（53 条被拦、澳门 24 条入库并渲染中文）。
- ~~文档：CHANGELOG 未记~~：已记 v1.146 / v1.148 / v1.149。
- ~~前端【探索】与【获取列表】共用一个端点（`start` 区分）~~：已记入 v1.146，未收到反对。

## 下一步

1. 起本地服务 + CDP Chrome，跑一次真实抓取验收（>50 与 ≤50 两条路径）。
2. 视需要补 CHANGELOG 一行；其余文档按 doc-policy 判据决定是否改。
3. 若 `start` 字段口径不接受，拆成两个端点或前端改为先取链接再单独 POST。

## 约束

- 取 Numista 页面只能走 `connect_over_cdp`，禁止普通 HTTP（403）。
- 探索数据只作展示，字段不完整；加入队列后仍走正常 4 步采集。
- SQL 只写在 `app/repository.py`，建表在 `app/models.py`；翻译映射不要散落在路由里。
- 不改既有文案与测试断言，不做无关重构。

## 读取起点

- `app/coins/numista_web.py`：`catalogue_url`（217-252）、`parse_catalogue_index`（255-306）、`fetch_catalogue_page`（400-433）
- `app/config.py`：`ExploreCatalogueConfig`（135-151）
- `app/coins/numista.py`：`ISSUER_CODES`（65-126）
- `app/templates/coins/ingest.html`、`app/routers/coins.py` 的 `ingest_page`

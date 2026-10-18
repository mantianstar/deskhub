# 变更记录（CHANGELOG）

> **用途**：只记「什么时候、为什么、改了什么」——这是**唯一**的时间线文件。
> [spec.md](./spec.md) / [plan.md](./plan.md) / [spec/](./spec) / [plan/](./plan) 只写**当前状态**，不写演化史：
> 新需求进来时**原地改写**受影响的正文，过程与理由记到本文件。
>
> **约定**：新条目加在**最上方**（最新在前）；每条写清「起因 → 改了什么 → 验证结论」，
> 技术事实（接口行为、站点限制这类长期有效的结论）落进 plan，本文件只留链接与结论概要。
> 每条末尾标出受影响的 spec / plan / tasks 文件。

---

## v1.16 — 2026-10-18 收藏画像年份规格字典补备注与行底色

**起因**：年份规格字典看不到单版备注；缺失和需升级的年份混在表格里，翻表时不易分辨。

**改了什么**

- 年份规格字典新增「备注」列，取 `coin_issues_cn.comment`。
- 缺失年份行、需升级年份行分别加不同底色（`--year-missing-bg` / `--year-upgrade-bg`）。

**验证结论**

- `build_year_specs` 用例补备注断言，收藏画像页面 smoke 补备注列与两种行底色断言；`pytest` **306 passed**。

**影响**：[spec/coins.md](./spec/coins.md) §14.17.3。

## v1.15 — 2026-10-17 铜币拍卖图片完整显示 + 点击放大

**起因**：专场卡片与拍品页的图片被裁成固定高度（`object-fit: cover`），币的边缘看不全；缩略图又小，细节认不清。

**改了什么**

- 专场卡片的 4 张商品图、拍品页的最多 3 张图改为 `object-fit: contain`，整张图完整显示。
- 图片点击放大：灯箱统一放到 `base.html`（点缩略图开、点遮罩 / 再点图 / Esc 关），专场卡片内的图片点击不触发卡片跳转；系列详情页复用同一份灯箱。

**验证结论**

- 专场列表 / 拍品页用例补图片可放大断言；`pytest` **306 passed**。

**影响**：[spec/coins.md](./spec/coins.md) §14.7。

## v1.14 — 2026-10-17 铜币拍卖按截止日期筛、收藏画像默认已命中

**起因**：专场列表只能看全部历史专场，翻不到指定一天的拍场；收藏画像打开时混着未命中的推荐项，干扰当日关注。

**改了什么**

- `/coins`：新增「拍卖截止日期」控件，默认选中业务时区的今日；只列截拍时间落在该日的命中专场。
- `/coins/collection`：「是否命中」默认「已命中」；「全部」改为显式选项 `hit=all`，翻页保留该状态。

**验证结论**

- 新增 1 个用例覆盖日期控件默认值与按日期过滤，原筛选用例补默认已命中 / 切全部断言；`pytest` 305 → **306 passed**。

**影响**：[spec/coins.md](./spec/coins.md) §14.7 / §14.17.6、[plan/coins.md](./plan/coins.md) §18.8 / §18.17.6。

## v1.13 — 2026-10-17 收藏画像改单表 + 筛选，成员候选加执政者

**起因**：三层推荐分成三块列表，看不出同一系列年份的整体分布，也没法按「系列 / 推荐原因 / 今日是否命中」收窄；
添加成员时同国家同面值下候选人太多，但缺了最关键的执政者维度。

**改了什么**

- `/coins/collection`：三层推荐合并为一张表，一个年份一行，带表头与「今日命中」列；
  三条优先级规则说明移到表格上方；新增系列 / 推荐原因（稀缺年份、差品升级、相似推荐）/ 是否命中筛选，
  推荐列表改为单表 20 条/页，筛选条件随翻页保留。
- `/coins/collections`：加入成员的候选筛选新增执政者，默认取收藏系列名称里的执政者（可切「全部」）；
  切换国家时重置为「全部」。
- 删除推荐列表改用表格后不再使用的 `coin-rec-*` / 匹配拍品明细样式。

**验证结论**

- 新增 2 个用例覆盖推荐表三项筛选、执政者候选筛选；`pytest` 303 → **305 passed**。

**影响**：[spec/coins.md](./spec/coins.md) §14.7 / §14.17.6、[plan/coins.md](./plan/coins.md) §18.17、[tasks/coins.md](./tasks/coins.md) M25-G。

## v1.12 — 2026-10-17 收藏画像 / 系列映射页面重排

**起因**：两页排版臃肿、信息主次不分，添加成员的候选下拉过长，推荐结果没有分页。

**改了什么**

- `/coins/collections`：规则与新建表单折叠；系列默认只显示两行摘要，成员表 / 添加 / 删除收进展开区；
  列表 20 个/页；添加成员改为「国家 + 面值」两级筛选，默认取收藏系列自身的国家 / 面值，
  候选下拉与成员表都显示年份范围。
- `/coins/collection`：推荐结果优先，三层各 20 条/页；9 列表格改为两行式紧凑行，
  命中拍品默认显示 2 条；收藏系列画像压缩为摘要行（10 个/页），展开看年份 chips 与规格字典。
- 两页内容区加宽到 1040px；新增页签、折叠摘要、年份 chip 样式；加入 / 移除成员后保持该系列展开。

**验证结论**

- 新增 3 个用例覆盖国家 / 面值候选筛选与两页分页；`pytest` 300 → **303 passed**。
- `curl` 实测两页 200；日本 / 2 钱筛选后候选只剩 2317，年份范围 1873–1892 正常渲染。

**影响**：[spec/coins.md](./spec/coins.md) §14.7 / §14.17.6、[tasks/coins.md](./tasks/coins.md) M25-F。

## v1.11 — 2026-10-17 铜币收藏画像与三层推荐

**起因**：原有推荐基于历史订单抽出的标签，无法回答「这个系列还缺哪一年、哪一年品相该升级」；
要求基于 Numista 目录 + 历史订单品相构建收藏系列画像，并生成三层优先级目标购买推荐；
同时采集只保留当日截拍的店铺。

**改了什么**

- 新增 `coin_collection_series` / `coin_collection_series_members`（`schema_version` 8 → 9，纯新增表），
  页面 `/coins/collections` 手工把多个 Numista series 合并成一个收藏系列。
- 新增 `app/coins/collection.py`：Bronze/Copper 过滤、单版 Proof 过滤、尺寸分档
  （中 [25,30) / 大 [30,35) / 超大 [35,∞)，<25 记「小」）、同年取最大发行量、
  已有 / 缺失 / 需升级年份（仅人工品相「差」）、未映射系列按合并规则自动成组。
- 新增 `app/coins/recommend.py`：P1 位置权重 × 稀缺权重（Frequency/100 → rarity）、
  P2 品相敏感度 × 稀有度（每系列前 3）、P3 跨系列相似兜底；日本年号翻译
  （`明治6年` / `明治六年`）与面值写法变体；推荐项与当日拍品匹配。
- 采集：`SessionCard` 增加 `end_at`，`pipeline._collect` 只抓业务时区「今天」截拍的专场；
  匹配优先走收藏画像，画像为空时回退旧标签；查价使用推荐项关键词。
- 页面 `/coins/collection`：收藏系列画像 + 三层推荐 + 当日命中拍品；子导航补两个入口。

**验证结论**

- 新增 `tests/test_coins_collection.py` 14 例：分档边界、材质/精制过滤、同年最大发行量、
  缺失/升级、P1/P2/P3 排序、`明治六年二钱` 匹配、映射 CRUD、两个页面 smoke。
- `pytest` 286 → **300 passed**。

**影响**：[spec/coins.md](./spec/coins.md) §14.4 / §14.5 / §14.7 / §14.17、[plan/coins.md](./plan/coins.md) §18.17、[tasks/coins.md](./tasks/coins.md) M25。

---

## v1.10 — 2026-10-17 品相录入：年号纪年取年份数字

**起因**：品相录入页把「明治十五年」原样展示，与 Numista 的 `year`（15）对不上，没法按年份核对。

**改了什么**

- `grades.normalize_year`：年号纪年取年号后的年份数字（中文数字转阿拉伯，`元` 按 1），
  如 `明治十五年` → `15`、`民国31年` → `31`、`康德元年` → `1`；只有年号没有年份数字（`光绪`）仍原样保留。
  页面展示 / 去重键 / 查询口径共用这一份实现。

**验证结论**

- 真实库：组合 845 → **842**（`明治15年` / `明治十五年` 等同值合并），`日本 / 15 / 2 钱` 4 条订单合成一行。
- `pytest` **286 passed**（更新年份统一用例：传统年号取数字、仅年号原样）。

**影响**：[spec/coins.md](./spec/coins.md) §14.16、[plan/coins.md](./plan/coins.md) §18.16、[tasks/coins.md](./tasks/coins.md)。

---

## v1.9 — 2026-10-17 系列详情：单版年份并显公历

**起因**：年号纪年的单版（如日本明治 `6` 年）在「各年份信息」表里只显 `6`，看不出对应公历；
要求 `year` 与 `gregorian_year` 不一致时显示 `year(gregorian_year)`，一致时只显 `year`。

**改了什么**

- `coin_issues` / `coin_issues_cn` 新增 `gregorian_year`（`schema_version` 7 → 8，登记 `app/migrations/v8.py`）。
- 采集链路存下该字段：`IssueInfo` / `CoinIssue` / repository 的 upsert 列与行映射；`year` 仍存 Numista 原值。
- 详情页年份列：`year != gregorian_year` → `year(gregorian_year)`，否则只显 `year`。

**验证结论**

- 重新采集系列 **2317**（2 Sen - Meiji）12 条：`6 → 1873`、`10 → 1877`、`25 → 1892`；页面渲染 `6(1873)`。
- 系列 **670** 未重采（`gregorian_year` 为空）→ 仍只显 `1895`，不受影响。
- `pytest` 284 → **286 passed**（新增：解析保留公历年、页面并显 / 一致时不并显）。

**影响**：[spec/coins.md](./spec/coins.md) §14.13 / §14.14、[plan/coins.md](./plan/coins.md) §18.13、[tasks/coins.md](./tasks/coins.md)。

---

## v1.8 — 2026-10-16 品相录入：三元组统一 + 新增组合 + 整行删除 + 品相查询

**起因**：品相录入页上线后用户连提四项 —— ① 年份与面值要「参考 Numista 做统一处理」（如 `1910`、`1 便士`）；
② 历史订单里没有的组合也要能录入；③ 国家也要统一（`英属新西兰` / `英属澳大利亚` 去掉「英属」）；
④ 历史错误数据要能**整行删除**，且删除要带确认。另补一项：加品相查询条件。

**改了什么**

- **新增 `app/coins/grades.py`**（纯函数，不依赖 FastAPI / DB）：`normalize_year`（去「年 / 年度」后缀只留数字，
  年号原样）、`normalize_denomination`（前导数字转阿拉伯、与单位间加空格；认不出前导数字的原样保留）、
  `build_combos`（按统一后的三元组去重 + LEFT JOIN 已录品相 + 收进「无订单的手工组合」）、
  `filter_combos`（查询词同口径统一后宽松包含）、`countries_of` / `denominations_of`（候选）。
  年号（`康德四年`）与 `当制钱十文` 这类原样保留。
- **统一后的值 = 去重键 = 落库键**：`1910` 与 `1910年`、`一分` 与 `1分` 合并成一行；`coin_orders` 一行不改。
- **去重从 SQL 挪到模块**：`repository` 去掉 `list_coin_order_combos` / 候选两个函数（收敛为纯读写），
  分组与合并全在 `grades.py`，`app.textmatch.contains` 复用同一份匹配口径。
- **可手工新增组合**：新增 `POST /coins/grades/add`（国家 / 年份 / 面值 / 品相四项必填，统一后 upsert）；
  新增的组合即使没有历史订单也出现在列表里。页面加「新增组合」表单与统一口径说明。
- **保存前再过一遍统一**：`POST /coins/grades/save` 的提交值落库前统一（幂等，防手改表单）。
- **国家也统一**：去殖民时期前缀（`英属新西兰` → `新西兰`、`英属澳大利亚` → `澳大利亚`、`荷属东印度` → `东印度`）；
  真库里 182 个国家里有 36 个带 `英属 / 法属 / 荷属 / 葡属 / 德属` 前缀。
- **整行删除（历史错误数据）**：行尾「删除」→ **页内二次确认**（第一次点变「确认删除」并给「取消」，再点才提交），
  删掉该组合在 `coin_orders` 里的**全部订单**（含不同写法）+ 该组合的品相记录；库里有缺任一维度的行不显示、也删不到。
- **新增 `POST /coins/grades/delete`**：按统一后的组合反查要删的订单 id（`grades.order_ids_for_combo` +
  `repository.list_coin_order_keys` / `delete_coin_orders`），删除后重定向回原筛选结果。
- **品相查询条件**（补）：`grades.filter_combos` 加 `grade`（空 = 不限，其余按 好 / 普通 / 差 精确相等）；
  `GET /coins/grades` 读 `grade`；查询表单加品相下拉（全部 / 好 / 普通 / 差）。

**验证结论**

- 真实库：1567 单 → **845 组合**（原始写法去重 953 → 仅统一年份 / 面值 867 → 再加国家前缀 **845**，
  即国家前缀合并掉 22 组）；`¼安娜` → `四分之一 安娜`、`壹圆` → `1 圆`、`英属海峡` → `海峡`。
- 实测：查询「英国 + `1910年`」命中 1 组（`1910`）；POST `/coins/grades/add`（`1910年` / `1便士`）→ 302，
  落库 `英国 / 1910 / 1 便士`；页面 845 行各带一个「删除」按钮；按品相筛（真实库 853 组合 / 已录 163）`grade=好` → 47 组。
- `pytest` 274 → **284 passed**（新增：年份 / 面值 / 国家统一、同值合并、手工新增、删整行（连带订单 / 手工组合 / 参数校验）、品相筛选）。

**影响**：[spec/coins.md](./spec/coins.md) §14.16、[plan/coins.md](./plan/coins.md) §18.2/§18.16、[tasks/coins.md](./tasks/coins.md)、[README.md](./README.md)。

---

## v1.7 — 2026-10-16 历史购买品相录入

**起因**：历史订单（`orders.xlsx`）只有订单名与价格，硬币的**品相**没地方记；
用户要求加一个页面：把历史记录按「国家 + 年份 + 面值」先去重，用国家 / 年份 / 面值查询，
结果为「国家 + 年份 + 面值 + 品相」，品相由用户下拉选（好 / 普通 / 差），点保存写库。

**改了什么**

- **新增独立表 `coin_purchase_grades`**（`(country, era, denomination)` 唯一，一档 `grade`），`schema_version` 6 → 7
  （纯新增表 → 登记进 `db.FORWARD_ADDITIVE_UPGRADES`，无需 ALTER）。
  **不写 `coin_orders.condition`** —— 那是订单级、LLM 抽的包浆描述，覆盖会污染画像、影响推荐。
- **去重查询**：`repository.list_coin_order_combos` 按「国家 + 年份 + 面值」分组（**只取三者齐全**的行），
  LEFT JOIN 已录品相；三条件做宽松包含匹配。另有 `upsert_coin_purchase_grade` / `delete_coin_purchase_grade` / 两个候选下拉。
- **页面**：`GET /coins/grades`（查询表单 + 去重组合表 + 行内品相下拉 + 底部「保存」）、
  `POST /coins/grades/save`（逐行 upsert；选「未录入」即清除；键不完整 / 品相非法 → 400、整批不落库、
  302 回原筛选结果）；新建 `coins/grades.html`，`sessions/info/manage.html` 子导航补「品相录入」入口。
- **文档**：spec §14.16、plan §18.16、tasks M23 原地新增（编号顺延）。

**验证结论**

- 真实库（1567 条订单 → **953 个组合**）：`/coins/grades` 200；筛「东加勒比」出 2 个组合；
  POST 保存 302、落库 2 行、页面回显 `selected`；老库启动日志 `schema 版本向前升级：6 → 7`。
- `pytest` 268 → **274 passed**（新增 6 例：去重 / 筛选 / 页面下拉 / 保存与清除 / 非法品相 400）。

**影响**：[spec/coins.md](./spec/coins.md) §14.16、[spec.md](./spec.md)、[plan/coins.md](./plan/coins.md) §18.2/§18.16、[plan.md](./plan.md)、[tasks/coins.md](./tasks/coins.md)、[tasks.md](./tasks.md)。

---

## v1.6 — 2026-10-15 信息页扩充 + 翻译改大模型

**起因**：用户连续四项要求 —— ① 管理页加「获取年份估值」「下载图片」；② 详情页图片点击放大；
③ 年份统计有误（「英国 · 1 便士 · 1895–1901」应是 **7 个年份、15 个版别**，原显示 15）；
④ 「各年份信息」与「各年份估值」合并为一张表。随后又补两项：管理页看不出「有没有年份信息 / 估值 / 图片」、
重跑是否覆盖历史；「澳大利亚 · 1 便士 · 1911–1936」的详情表被撑开（最右列被裁）。
最后一项：**描述 / 备注 / 名称改大模型翻译**。

**改了什么**

- **年份统计口径**：`coin_series_issue_counts` → `coin_series_issue_stats`（同时给 `COUNT(DISTINCT year)` 与 `COUNT(id)`），
  两页显示「N 个年份 · M 个版别」。
- **详情页合并表**：以 `coin_issues` 行为主、按 `numista_id` 对齐估值，合成一张 13 列「各年份信息」表
  （年份 / 厂标 / 发行量 / G…UNC / 目录号 / 备注 / 稀有度）。
- **管理页**：结果行加「③ 获取年份估值」「④ 下载图片」两个按钮（复用既有 CDP 链路）+ 「已采集状态」三标记
  （年份 / 估值 / 图片）+ 明文「重复操作 = 按编号覆盖更新」。
- **图片点击放大**：详情页原生 JS 灯箱（无框架、无 CDN）。
- **宽表修正**：13 列表收窄内边距（`10px 12px` → `6px 8px`），表宽从 773px 回到 738px（= 内容区），
  最右「稀有度」不再被裁。
- **管理页缩略图**：结果行「系列」列内渲染正 / 反图缩略图，**不新增表格列**（多 144px 会撑破内容区、横向滚动藏住按钮）。
- **大模型翻译**：新增 `app/coins/translate_llm.py`；枚举字段仍走词表（它还是采集过滤的依据），
  只有 `title / obverse_desc / reverse_desc / comments` + 单版 `comment` 交大模型；一个系列的长文本一次请求、
  全部单版备注一次请求；无 Key / 失败回退词表；新增 `coins-translate` 重翻存量（读英文表、不重采）、
  并在中文表落库前**剥 HTML**（Numista 的 `comments` 是 HTML 片段，Jinja 自动转义会把标签显示成字面文本）。

**实测结论**

- 系列 670 → `7 个年份 · 15 个版别`；`/coins/series/1269` 表宽 **738 = 容器 738**（修前 773，溢出 35px）。
- 管理页三行状态显示正确（`年份 7 个 · 15 版 / 估值 15 条 / 图片 2/2` 等）；缩略图 6 个 `<img>`、0 占位、0 破图。
- `coins-translate`（配好 Key）真实跑通：系列级 2 次请求、全量 7 次请求 200；中文表 HTML 标签 **0 处**、英文表保留原文；
  中英行数 4/4、110/110（不重采、不加行）。
- **踩坑（实测）**：`conftest` 的「测试禁用 LLM」夹具用 `delenv` **没挡住** —— `config.load()` 会
  `load_dotenv(".env", override=False)`，变量不存在时 Key 被从 .env 读回来，全量测试真打到了线上模型
  （7s → 54s）。改为 `setenv(..., "")` 后正常（见 [plan/coins.md](./plan/coins.md) §18.13.4）。
- **踩坑（实测）**：`.coin-lightbox` 用 `display:flex` 会盖过 `[hidden]` 的默认隐藏 → 灯箱一开始就常驻显示，
  需补 `.coin-lightbox[hidden] { display:none }`。

**测试**：`pytest` 243 → **268 passed**。

**影响**：[spec/coins.md](./spec/coins.md) §14.13/§14.14/§14.15、[plan/coins.md](./plan/coins.md) §18.13.4/§18.14/§18.15、[tasks/coins.md](./tasks/coins.md)。

---

## v1.5 — 2026-10-15 信息页面 + 图片本地缓存 + 采集过滤

**起因**：参考库只能靠 CLI 与 sqlite 查看，无法浏览；用户要求搬上网页（先查询 → 再详情 → 管理页两步采集）。
预览时发现 Numista 图片带 `cross-origin-resource-policy: same-origin`，跨站 `<img>` **必然拦截**、
裸 HTTP 又吃 Cloudflare 403 → 只能本地缓存。随后三项修正：信息页系列图不显示、表单按钮被拉成高块、
「面值条件没用」（查「英国 1904 1便士」把「半便士」也列出来）+ 要国家可输入 + 要材质条件。
最后用户明确：「多余的系列对我来说没有用」→ 采集时挡掉不匹配的。

**改了什么**

- **三个页面**：`/coins/info`（查询，只读本地）、`/coins/series/{id}`（详情 + 各年份信息）、`/coins/manage`（两步采集，
  按钮 1 取系列 → 结果行按钮 2 取年份）；采集**零改动复用** CLI 的同一批函数；同步 `await` + 错误就地提示。
- **图片本地缓存**：新增 `app/coins/numista_web.py` 的 `download_series_images` / `cached_image`，
  路由 `GET /coins/series/{id}/image/{side}`；页面**缓存过才渲染 `<img>`**，未缓存给原图链接 / 占位块，不出现破图。
- **采集过滤**：新增 `app/textmatch.py`（归一化 + 数字边界），采集与页面**共用一份**；
  `collect_series` 改为「取详情后按面值 / 材质过滤，不匹配不落库」；新增 `delete_coin_series` 清理存量。
- **筛选与可输入**：国家 / 面值 / 材质改 `<input list>` + `<datalist>`，面值 / 材质可筛也可输入。

**实测结论**

- **接口能力（打真实 v3 接口）**：`/types` 的 `q` 只做模糊匹配；**面值不可筛**（`denomination` / `value` / `currency`
  传了被静默忽略）；只有 `material=<id>` 能按材质筛（`/materials` 给 71 个材质及 id）。
- 过滤生效：`1便士` 挡掉 `½ Penny`、加 `青铜` 挡掉银币、`半便士` 命中 `½ Penny`；
  清掉误采的 4 个系列（871 / 1867 / 4576 + 56938），英国库 **9 → 5 条**。
- 真实 CDP 下载系列 670 正 / 反图（318540 / 332675 字节），图片路由 `200 image/jpeg`。
- **踩坑（实测）**：取图不能用 `context.request.get()`（Playwright 自己的请求过不了 Cloudflare，必 403）→
  改为**真实页面导航**读字节，且先打开类型页过一次校验；原 `_wait_for_table` 轮询 `page.content()` 会抛
  「page is navigating and changing the content」（校验页自我刷新）→ 改为 `wait_for_selector("tr.date_row")`。
- **踩坑（实测）**：管理页**根本没把面值传进本地筛选**（`denomination` 只拿去调接口）；且库值带空格
  （`1 便士` vs 用户输入 `1便士`）→ 必须先归一化空白，同时按数字边界保护（`1便士` 不许咬进 `11便士`）。
- **按钮「不和谐」的成因**：`.search-row` 是 flex 未设对齐 → 按钮被 `stretch` 拉到整行高；
  年份 / 面值输入框的 `.search-input`（`flex:1`）落在纵向 flex 里 → `flex-basis:0%` 压过 `height`。三者都要点到位才等高。

**测试**：`pytest` 210 → **235 passed**（页面 10 + 筛选 5 + 过滤 5）。

**影响**：[spec/coins.md](./spec/coins.md) §14.13/§14.14/§14.15、[plan/coins.md](./plan/coins.md) §18.13.3/§18.14/§18.15、[tasks/coins.md](./tasks/coins.md)。

---

## v1.4 — 2026-10-14 各品相估值 + Frequency

**起因**：用户要「每个年份 × 每个品相」的价格，以及 Frequency 列。

**改了什么**：新增 `coin_issue_values` 表（`schema_version` 4 → 5，纯新增表）+ `app/coins/numista_web.py`
（`parse_type_page` 纯函数 + `fetch_type_page` CDP 取页 + `collect_issue_values` 落库）+ CLI `coins-values`。

**实测结论**

- **接口里没有这两项**（`/types/{id}` 与 `/types/{id}/issues` 字段并集里既无 frequency 也无按品相的值，
  `/types/{id}/issues/{issue_id}/prices` 有价格但要逐单版 1 次请求、且无 Frequency）→ 改为读类型页一次拿全。
- **页面取数把能试的都试了，只有一条路通**：`curl` / `httpx`（完整浏览器头）/ `curl_cffi`（指纹伪装）一律 403
  （Cloudflare 的 `Checking connection…`）；Playwright 自己 launch 的 Chrome（headless 与 headful）卡在校验页；
  `--remote-debugging-port` + `connect_over_cdp` 挂上用户自己的 Chrome **成功**。
- 行 ↔ 单版编号：每行后面紧跟的 `<tbody id="collec_line{编号}">` 就是 Numista 单版编号，
  与 `/types/670/issues` 的 15 个 id 完全一致（落库后 join：**0 行对不上**）；编号 0 的占位行跳过。
- 币种按**访客币种**渲染（本机 CNY），随数据读出来存下（不写死）。

**测试**：`pytest` 204 → **210 passed**。

**影响**：[spec/coins.md](./spec/coins.md) §14.13、[plan/coins.md](./plan/coins.md) §18.13.2、[tasks/coins.md](./tasks/coins.md)。

---

## v1.3 — 2026-10-14 参考库补全（目录号 / 加列 / 中文表 / 风控）

**起因**：用户四项要求 —— ① 风控（额度 2000/月）② 目录号取全 ③ 加列 ④ 中文表。

**改了什么**：`references` 每条取 `目录代码#编号`；`coin_series` 加 `comments`/`mints`、`coin_issues` 加 `catalog_no`
（`schema_version` 3 → 4，登记 `app/migrations/v4.py` 幂等 `ALTER`）；新增 `coin_series_cn` / `coin_issues_cn` /
`coin_api_usage`；`translate.py` 内置词表做英文→中文；`_guard_quota` 做单轮 + 月度双上限。

**实测结论**

- 实测 Numista **响应不带 `X-RateLimit` / `Retry-After`** → 只能自管：间隔 1→2s + 单轮 40 + 月度 1500（留 25% 余量，
  按业务时区切月），每次请求落库计数，CLI 末尾打印用量。
- 670 目录号由 `790`（只 KM）变 **`KM#790,Sp#3961`**；`mints` 实测 `["Royal Mint (Tower Hill)"]`。
- 中文表：`title=1 便士 - 维多利亚 (第三 肖像)`、`composition=青铜`、`mints=["皇家铸币厂 (伦敦塔山)"]`；
  长描述只做术语替换，未收录片段保留英文（不做假翻译，已与用户确认）。
- 迁移：老库 `schema_version` 3 → 4，日志列出加列项；`plan.md` 的「手工执行 migration」改为**登记 + 幂等 ALTER**。

**测试**：`pytest` 198 → **204 passed**。

**影响**：[spec/coins.md](./spec/coins.md) §14.13、[plan/core.md](./plan/core.md) §6.2、[plan/coins.md](./plan/coins.md) §18.13.1、[tasks/coins.md](./tasks/coins.md)。

---

## v1.2 — 2026-10-14 铜币参考库（Numista）

**起因**：管「硬币本身」的目录信息（系列 / 单版）—— 与拍卖互不依赖的第二条采集。

**改了什么**：`coin_series` / `coin_issues` 两表（`schema_version` 2 → 3，纯新增表）+ `app/coins/numista.py`
（中文→英文映射 + 纯函数解析 + `NumistaClient` + 采集编排）+ CLI `coins-series` / `coins-issues` + config `coins.numista`。
**只交付 CLI + 落库，不做浏览页面**（用户选择）。

**实测结论（参考代码与真实接口不符，三点都用实测纠正）**

1. 参考实现用 `min_year`/`max_year` —— **被忽略**；真正生效的是 **`year`（单年）**。
2. 参考实现用 `/types/{id}?includeIssues=1` —— **该参数无效**；单版在**独立端点** `/types/{id}/issues`，返回**裸数组**。
3. 参考实现用 `issuer=united_kingdom` —— **非法**；发行方 code 是法文式 slug（英国=`royaume-uni`），
   且 L1 的 `united-kingdom` 会含百慕大等属地。

端到端（真实 API）：`coins-series --country 英国 --year 1900 --denomination 1便士` 命中 3 个系列；
`coins-issues --series-id 670` 取回 15 条单版（含 1900 年发行量 31,778,100）；库自动 2 → 3 升级。

**测试**：`pytest` 180 → **198 passed**。

**影响**：[spec/coins.md](./spec/coins.md) §14.13、[plan/coins.md](./plan/coins.md) §18.13、[tasks/coins.md](./tasks/coins.md)。

---

## v1.1 — 2026-10-09 ~ 10-12 铜币拍卖（M7~M10）

**起因**：铜币拍卖由占位页升级为正式模块（原先是「不做」的禁止项，v1.1 局部豁免）。

**M7 画像（零风险先做）**

- `coins-parse-orders`：1567 条订单**全解析（0 失败）**、979 个画像标签（英国 297 / 1便士 219…），
  `1538/1567 条至少抽到一个字段（98%）`。
- **踩坑并修好**：LLM 单次请求 30s 超时把大多数抽取批打成 `ReadTimeout`（79 批里连续多批被跳过）→
  新增 `llm.timeout_seconds`（默认 120）、`batch_size` 降到 20、新增 `concurrency: 6`、抽取改并发 + `gather` 后串行落库。
  改完 **79 批全部成功**。

**M8 采集（最大不确定性，单独成块）**

- 第一版用 Playwright + 人工过阿里云滑块（CDP 挂用户 Chrome）；第一版解析器对着**手工构造的 fixture** 全错
  —— 正是不该拿手工 fixture 当站点兼容性证明。后按**真实 DOM** 重写，fixture 换成真实 HTML 切片。
- 修过一次「漏掉当日 / 进行中专场」：裸 `/auction` 只列未来开拍的场，且没翻页 → 改取两路并翻页。
- **2026-10-12 整块改版**：用户指出网页端**根本没有** `end_time`（截拍时间），并从 App 抓包给出网关
  `gapi.mxiqi.com`。实测网关**免登录、免签名**，普通 JSON POST 即可（App 那条请求带 AES 加密 body + Bearer，
  但同一接口普通 JSON 就能用）。于是采集 + 查价**全部改走 gapi**，Playwright / CDP / 登录态那套**整块删除**
  （连带删掉 `playwright` 依赖与两个 HTML fixture）。真实跑通 36 专场 / 3962 拍品；
  `沼泽如烟--第1230场` 截拍时间落库正确。

**M9 匹配 / 定价 / 页面**

- **踩坑并修好**：`极美品` 会被更短的 `美品` 先咬掉 → 改为按词长降序替换。
- **首次真实查价后发现两类错价**（都已修）：① 子串咬进更长数字（「5分」命中「25分」）；
  ② 关键词太泛（`1961` / `巴拿马 1967` → 把当前价 ¥330 的套币报成 ¥2）→ 加「报价前必须命中面值/品类」这道闸，
  没命中就**主动不报价**。修完 5 条推荐 → 3 条给出推荐价（带区间与样本数），2 条主动不报价。
- **查价限速**（用户要求防封号）：间隔 15s（±40% 抖动）+ 单轮上限 20，超出的自动推迟到下一轮。

**M10 收口**：`daily_coins`（08:00 独立 job，`coalesce` / `misfire_grace_time=3600` / `max_instances=1`），
与知识日报互不阻塞；导航「铜币」改正式入口。`pytest` 180 passed。

**影响**：[spec/coins.md](./spec/coins.md) §14.1~§14.12、[plan/coins.md](./plan/coins.md) §18.1~§18.12、[tasks/coins.md](./tasks/coins.md)。

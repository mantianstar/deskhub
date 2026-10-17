# 铜币模块 — tasks

> 模块任务与验证证据。需求见 [spec/coins.md](../spec/coins.md)，实现方案见 [plan/coins.md](../plan/coins.md)。
> 通用执行规则（颗粒度 / 提交约定 / 全局禁止项）见 [tasks.md](../tasks.md) §0 与 [RULES.md](../RULES.md)。
> **「为什么改、踩了什么坑」的过程叙述统一在 [CHANGELOG.md](../CHANGELOG.md)**，本文件只留任务拆解与验证结论。
> 顺序（spec/coins.md §14.10）：**先画像 → 再打通采集 → 再匹配定价 → 最后页面**；高风险环节（采集 → 采集接口）单独成块、单独验证。

## 里程碑一览（M7 ~ M24）

| 主题 | 里程碑 | 状态 |
|---|---|---|
| 画像 | M7 | ✅ |
| 拍卖采集 | M8 | ✅ |
| 拍卖推荐与页面 | M9 | ✅ |
| 拍卖收口 | M10 | ✅ |
| 参考库（目录 / 单版 / 中文表 / 风控） | M11–M12 | ✅ |
| 参考库估值 + Frequency | M13 | ✅ |
| 信息页面（查询 / 详情 / 采集管理） | M14 · M16 · M17 · M19 · M20 · M22 | ✅ |
| 图片本地缓存 + 灯箱 | M15 | ✅ |
| 采集过滤 + 清理存量 | M18 | ✅ |
| 描述 / 备注 / 名称走大模型翻译 | M21 | ✅ |
| 历史购买品相录入 | M23 | ✅ |
| 单版年份并显公历 | M24 | ✅ |

---

## M7 — 画像（零风险，先做）

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M7-A | 数据层 + 解析 + 聚合（一次成文）：`coin_*` 七表 DDL + `schema_version` 1→2 + repository 读写 + `orders.xlsx` 解析 + LLM 抽字段 + 按维度聚合权重 | `app/models.py`、`app/db.py`、`app/repository.py`、`app/coins/profile.py` | 真实 `orders.xlsx` 1567 条全解析（0 失败）→ 979 个标签；重复导入不增行 |
| [x] M7-B | 页面 + CLI：`/coins/profile` 可编辑（启停 / 改权重 / 增删）+ `coins-parse-orders` / `coins-tags` | `app/routers/coins.py`、`app/templates/coins/profile.html`、`app/cli.py` | 浏览器实测四类操作都落库；`coins-tags` 打印「维度 / 值 / 权重」 |
| [x] M7-C | 测试 + 人工校对：`tests/test_coins_profile.py`；抽读标签、关掉明显错抽 | `tests/test_coins_profile.py` | `pytest` 全绿；抽读校对结论见 CHANGELOG |

## M8 — 拍卖采集（最大不确定性，单独成块）

> 方法论：**先抓一轮真实样本**（真实 JSON 响应存成 fixture），解析开发**全离线**，不在真实网络上反复调试。
> 采集走小程序网关 `gapi.mxiqi.com`（免登录、免签名、免浏览器）。

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M8-A | 采集（离线 payload 驱动）：**小程序网关客户端**（`ListAuction` 拿场 + `get` 拿专场/拍品）+ 纯函数解析 + 采集编排 | `app/coins/gapi.py`、`app/coins/pipeline.py`、`app/cli.py` | 真实响应字段驱动全部解析用例；`pytest` 全绿 |
| [x] M8-B | 真实端到端验证（一次）：`coins-fetch` 抓专场 / 拍品并落库 | — | 真实 36 专场 / 3962 拍品落库（免浏览器）；`沼泽如烟--第1230场` 截拍时间落库正确 |

## M9 — 拍卖推荐与页面（离线可测）

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M9-A | 匹配 + 定价（离线）：`matcher.py`（修饰词归一 + 命中权重和 ≥ 阈值）、`pricing.py`（关键词构造 + 中位数 / P25–P75 / 样本数 → `coin_price_refs`） | `app/coins/matcher.py`、`app/coins/pricing.py`、`app/repository.py` | 单测覆盖归一化、阈值边界、样本 <3 只给区间、0 样本不报价 |
| [x] M9-B | 两个页面：`/coins`（仅列命中场 + 空态）、`/coins/session/{id}`（店铺头 + 推荐拍品卡） | `app/routers/coins.py`、`app/templates/coins/sessions.html`、`app/templates/coins/session.html` | 真实数据渲染两页，字段对齐 `shop.png` / `item.png` |

## M10 — 拍卖收口

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M10-A | 调度 + 导航：`daily_coins`（08:00 独立 job，失败不传播）；`/coins` 由正式模块接管、`/funds` 仍占位 | `app/scheduler.py`、`app/main.py`、`app/templates/base.html`、`app/routers/placeholders.py` | 与 `daily_pipeline` 互不阻塞 |
| [x] M10-B | 文档回写 + 端到端收口 | spec / plan / tasks / README | spec↔plan↔tasks 无静默跑偏；`pytest` 全绿 |

## 参考库 M11–M12（目录 / 单版 / 中文表 / 风控）

> 独立于麦稀奇（拍卖）的第二条采集：从 numista.com 取「硬币本身」的目录信息（系列 / 单版）。

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M11-A | 参考库（一次成文）：`coin_series` / `coin_issues` 两表 + `schema_version` 2→3 + repository 读写 + `numista.py`（中文→英文映射 + 纯函数解析 + `NumistaClient` + 采集编排）+ config `coins.numista` | `app/models.py`、`app/db.py`、`app/repository.py`、`app/config.py`、`app/coins/numista.py`、`config/config.yaml`、`.env.example` | 真实「英国 1900 年 1 便士」查得系列 670（3 个系列卡）；系列 670 取回 15 条单版并落库 |
| [x] M11-B | CLI + 测试 + 文档：`coins-series` / `coins-issues`；`tests/test_coins_numista.py` | `app/cli.py`、`tests/test_coins_numista.py` | `pytest` 全绿（198 passed） |
| [x] M12-A | 列扩充 + 迁移机制 + 中文表 + 风控（一次成文）：`coin_series` 加 comments/mints、`coin_issues` 加 catalog_no、新增 `coin_series_cn`/`coin_issues_cn`/`coin_api_usage`、`schema_version` 3→4 + `app/migrations/v4.py` 幂等加列；`translate.py` 内置词表；`catalog_no` 取全部引用；`_guard_quota` | `app/migrations/v4.py`、`app/coins/translate.py`、`app/coins/numista.py`、`app/config.py`、`config/config.yaml` | 老库自动 3→4 加列成功；英文表 + 中文表同时落库；`catalog_no` = `KM#790,Sp#3961` |
| [x] M12-B | CLI 用量提示 + 测试 + 文档回写 | `app/cli.py`、`tests/test_coins_numista.py` | `pytest` 全绿（204 passed）；真实跑通月度计数 |

## 参考库估值 M13（各品相估值 + Frequency）

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M13-A | 估值 + Frequency（一次成文）：`coin_issue_values` 表 + `schema_version` 4→5 + repository 读写 + `numista_web.py`（纯函数解析 + CDP 取页 + 落库编排）+ `coins-values` CLI + config `cdp_url` | `app/coins/numista_web.py`、`app/repository.py`、`app/cli.py`、`config/config.yaml`、`requirements.txt` | 真实系列 670：15 条估值 + Frequency 全部落库（币种 CNY），逐行与页面一致；与 `coin_issues` 对不上 0 行 |
| [x] M13-B | 测试 + 文档回写 | `tests/test_coins_numista_web.py`、`tests/fixtures/numista_type_670.html` | `pytest` 全绿（210 passed）；老库 4→5 自动升级 |

## 信息页面 M14 · M16 · M17 · M19 · M20 · M22

> 把参考库从 CLI 搬上网页：`/coins/info`（查询）、`/coins/series/{id}`（详情 + 各年份信息表）、`/coins/manage`（两步采集）。
> 期间连着修正了筛选、图片、年份统计与宽表。

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M14-A/B | 数据 + 页面（一次成文）：repository 补 `list_coin_series_countries` / `search_coin_series` / `coin_series_issue_counts`；`routers/coins.py` 加 5 条路由；新建 `coins/{info,series,manage}.html` + 子导航 + `style.css` 追加类 | 同左 | 三页全 200 无 traceback：`/coins/info` 3 张系列卡；`/coins/series/670` 年份表 15 行 + 估值表 15 行；`pytest`（220 passed） |
| [x] M16-A | 信息页图 + 按钮（一次成文）：`info_page` 补 `series_images`；`info.html` 缓存过才渲染本地图、其余占位；表单行加 `search-row-labeled`（底对齐 + 同为 32px） | `app/routers/coins.py`、`app/templates/coins/{info,manage}.html`、`app/static/style.css` | `pytest`（225 passed）；Playwright 实测两页字段与按钮 height 全 32px、底边同线；无图系列给占位、无破图 |
| [x] M17-A/B | 筛选 + 可输入（一次成文）：`search_coin_series` 补 `denomination` / `composition`（归一化 + 数字边界）；新增面值 / 材质候选函数；两页透传；国家 / 面值 / 材质改 `<input list>` + `<datalist>` | `app/repository.py`、`app/routers/coins.py`、`app/templates/coins/{info,manage}.html`、`app/static/style.css` | 真实库：`英国+1904+1便士` → 只回 857 / 13222（**不含半便士**）；`青铜` → 857 / 1867；`pytest`（230 passed） |
| [x] M19-A/B/C | 合并表 + 年份统计修正 + 灯箱标记：`coin_series_issue_counts` → **`coin_series_issue_stats`**（`COUNT(DISTINCT year)` + `COUNT(id)`）；管理页加两个 POST；`series.html` **两表合并为「各年份信息」**（13 列）+ 图片缩放；`manage.html` 结果行三按钮 | `app/models.py`、`app/repository.py`、`app/routers/coins.py`、`app/templates/coins/{info,series,manage}.html`、`app/static/style.css` | 系列 670 → `7 个年份 · 15 个版别`；合并表 13 列 15 行；灯箱 Playwright 5 步全过、0 控制台错误；`pytest`（241 passed） |
| [x] M20-A/B | 管理页采集状态 + 宽表修正：`coin_series_value_counts`；`_render_manage` 补 `value_counts` / `series_images`；结果行加「已采集状态」三标记 + 覆盖更新说明；`series.html` 表收窄内边距 | `app/repository.py`、`app/routers/coins.py`、`app/templates/coins/{manage,series}.html`、`app/static/style.css` | 三行分别显示 `年份 7 个 · 15 版 / 估值 15 条 / 图片 2/2`、`9 个 · 11 版 / 未采 / 未采`、`未采 × 3`；`/coins/series/1269` 表宽 **738 = 容器 738**；`pytest`（243 passed） |
| [x] M22-A/B | 管理页结果显示图片：结果行「系列」列内（系列名上方）渲染正 / 反图缩略图（已缓存 `<img>`、未缓存同尺寸占位），**不新增表格列** | `app/templates/coins/manage.html`、`app/static/style.css` | `/coins/manage?country=英国` 3 行各 2 张缩略图、0 占位、破图 0；表宽 **738 = 容器 738**；`pytest`（268 passed） |

## 图片本地缓存 M15（含灯箱）

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M15-A | 图片缓存（一次成文）：`numista_web` 补 `IMAGE_DIR` / `image_path` / `cached_image` / `_fetch_bytes_via_cdp` / `download_series_images`；CLI `coins-images --series-id [--cdp]`；路由 `GET /coins/series/{id}/image/{side}`；详情页「缓存过才 `<img>`、否则给原图链接」；去掉年份表图列 | `app/coins/numista_web.py`、`app/cli.py`、`app/routers/coins.py`、`app/templates/coins/series.html`、`app/static/style.css` | 未缓存：路由 404 + 页面给原图链接；**真实下载实测**：系列 670 正/反图落 `data/coin_images/`（318540 / 332675 字节），详情页 2 个 `<img>`、图片路由 `200 image/jpeg` |
| [x] M15-B | 测试 + 文档回写 | `tests/test_coins_pages.py` | `pytest`（224 passed；`IMAGE_DIR` → tmp、CDP 取字节被桩） |

## 采集过滤 M18（挡掉多余系列 + 清理存量）

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M18-A | 过滤 + 清理（一次成文）：新增 `app/textmatch.py`（归一化 + 数字边界，采集与页面**共用**）；`collect_series` 改为「取详情后按面值 / 材质过滤，不匹配不落库」；`search_coin_series` 改用 textmatch；新增 `delete_coin_series` | `app/textmatch.py`、`app/coins/numista.py`、`app/repository.py`、`app/routers/coins.py`、`app/cli.py` | **真实接口实测**：`q=1 penny&year=1904` → 1867(`½ Penny`) 被挡、857/13222(`1 Penny`) 入库；加 `青铜` → 13222(银) 被挡；查 `半便士` → 1867 命中 |
| [x] M18-B | 清理存量 + 测试 + 文档回写 | `tests/test_coins_numista.py` | 清掉误采的 4 个系列（871 / 1867 / 4576 + 56938），英国库 **9 → 5 条**；`pytest`（235 passed） |

## 大模型翻译 M21（描述 / 备注 / 名称）

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M21-A | 大模型翻译（一次成文）：新增 `app/coins/translate_llm.py`（纯函数解析 + 批量取数 + `retranslate` 存量重翻）；`numista.py` 的 `_store_series`/`_store_issues` 改异步、经 `_translator` 拿客户端（无 Key 给 None → 词表）、失败逐项回退；`collect_series`/`collect_issues` 加 `llm_client` 注入；CLI `coins-translate [--series-id N]` | `app/coins/translate_llm.py`、`app/coins/numista.py`、`app/coins/translate.py`、`app/cli.py` | 无 Key 时 `coins-translate` 打印提示并 **exit 1** |
| [x] M21-B | 剥 HTML + 测试 + 文档回写 | `app/coins/translate.py`、`app/coins/numista.py`、`tests/test_coins_translate.py`、`tests/conftest.py` | `pytest`（258 passed）；全量测试不发真实 LLM 请求 |

## 历史购买品相录入 M23（spec/coins.md §14.16）

> 历史订单按「国家 + 年份 + 面值」去重后，人工为每个组合录一档品相。
> 品相存**独立表**（不覆盖 `coin_orders.condition`、不影响画像与推荐）。只列三者齐全的组合。

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M23-A | 数据 + 页面（一次成文）：`coin_purchase_grades` 表 + `schema_version` 6→7（纯新增表，登记 `FORWARD_ADDITIVE_UPGRADES`）+ repository 读写（去重查询 / upsert / 删除 / 候选）；路由 `GET /coins/grades` + `POST /coins/grades/save`；新建 `coins/grades.html`；`sessions/info/manage.html` 子导航补「品相录入」；`style.css` 追加 `.coin-grade-table` / `.coin-grade-actions` | `app/models.py`、`app/db.py`、`app/repository.py`、`app/routers/coins.py`、`app/templates/coins/{grades,sessions,info,manage}.html`、`app/static/style.css` | 真实库（1567 单 → **953 组合**）：`/coins/grades` 200；筛「东加勒比」2 组合；POST 保存 302、落库 2 行、回显 `selected`；老库启动自动 6→7 |
| [x] M23-B | 测试 + 文档回写 | `tests/test_coins_pages.py`（去重 / 筛选 / 页面 / 保存 / 清除 / 校验 6 例）、spec/plan/tasks/CHANGELOG | `pytest`（268 → **274 passed**） |
| [x] M23-C | 年份 / 面值按 Numista 口径统一 + 可手工新增组合：新增 `app/coins/grades.py`（纯函数：`normalize_year` / `normalize_denomination` / `build_combos` / `filter_combos` / 候选）；repository 收敛为纯读写（去重从 SQL 挪到模块）；路由加 `POST /coins/grades/add`、保存前统一；模板加「新增组合」表单 + 统一口径说明 | `app/coins/grades.py`、`app/repository.py`、`app/routers/coins.py`、`app/templates/coins/grades.html` | 真实库：1567 单 → **845 组合**（原始写法去重 953 → 仅统一年份 / 面值 867 → 再加国家前缀 845）；`1910`/`1910年`、`一分`/`1分`、`英属新西兰`/`新西兰` 各并一行；新增「英国 1910年 1便士」→ 落库 `英国 / 1910 / 1 便士` |
| [x] M23-D | 测试 + 文档回写 | `tests/test_coins_pages.py`（统一 / 合并 / 手工新增 / 筛选等 11 例）、spec/plan/tasks/CHANGELOG | `pytest`（274 → **279 passed**） |
| [x] M23-E | 国家也统一 + 整行删除（一次成文）：`grades.normalize_country` 去殖民前缀（`英属`/`法属`/`荷属`/`葡属`/`德属`…）；repository 加 `list_coin_order_keys` / `delete_coin_orders`；`grades.order_ids_for_combo` 按统一后的组合反查订单 id；路由加 `POST /coins/grades/delete`；模板加「操作」列 + 页内二次确认 JS + 说明；`style.css` 加 `.btn-danger` | `app/coins/grades.py`、`app/repository.py`、`app/routers/coins.py`、`app/templates/coins/grades.html`、`app/static/style.css` | 真实库：候选国家 **155 → 140**（36 个 `X属` 前缀合并掉 22 组，组合数 867 → **845**）；页面 845 行各带一个「删除」按钮；删整行连带该组合全部订单（含不同写法）|
| [x] M23-F | 测试 + 文档回写 | `tests/test_coins_pages.py`（国家统一 / 删整行连带订单 / 删手工组合 / 参数校验 4 例）、spec/plan/tasks/CHANGELOG/README | `pytest`（279 → **283 passed**）|
| [x] M23-G | 品相查询条件：`filter_combos` 加 `grade`（空=不限，其余精确相等）；路由 `GET /coins/grades` 加 `grade` 参数；模板查询表单加品相下拉（全部 / 好 / 普通 / 差） | `app/coins/grades.py`、`app/routers/coins.py`、`app/templates/coins/grades.html`、`tests/test_coins_pages.py` | 真实库（853 组合 / 已录 163）：`grade=好` → 47 组合；空 → 853；`pytest`（283 → **284 passed**）|

## 单版年份并显公历 M24（spec/coins.md §14.13 / §14.14）

| # | 块 | 产出 | 验证 |
|---|---|---|---|
| [x] M24-A | 年份列并显公历：`coin_issues[_cn]` 加 `gregorian_year` + `schema_version` 7→8（`app/migrations/v8.py`）；`IssueInfo` / `CoinIssue` / repository upsert 与行映射带上该字段；`series.html` 年份列按「year == gregorian_year 只显 year，否则显 year(gregorian_year)」渲染 | `app/models.py`、`app/migrations/v8.py`、`app/repository.py`、`app/coins/numista.py`、`app/templates/coins/series.html`、`tests/test_coins_numista.py`、`tests/test_coins_pages.py` | 2317 重新采集（12 条，`6 → 1873`…`25 → 1892`），页面显示 `6(1873)`；670 页面仍只显 `1895`；`pytest`（284 → **286 passed**） |

---

## 未结项（需要时间 / 需要你）

| # | 事项 | 依赖 | 说明 |
|---|---|---|---|
| [ ] | 多日观察（spec/coins.md §14.12 第 6 条） | 时间 | 不代填 |
| [ ] | 匹配阈值 1.5 校准（spec/coins.md §14.11） | 真实推荐结果 | 当前偏松，会出现只靠年代命中的弱推荐 |

---

## 附录：验收对照

**拍卖（spec/coins.md §14.12）**

| 验收项 | 对应块 | 状态 |
|---|---|---|
| 1. `orders.xlsx` 能解析出订单、画像可编辑 | M7-A ~ M7-C | [x] 真实 1567 条全解析（0 失败）、979 个标签；`/coins/profile` 可编辑 |
| 2. 每天能采到当日专场与拍品 | M8-A/B、M10-A | [x] 走 gapi（免浏览器）真实抓到 36 专场 / 3962 拍品并落库 |
| 3. 画像能筛出相关专场，无命中为空态 | M9-A/B | [x] 真实数据下列表页只列命中场；空态用例覆盖 |
| 4. 推荐价带区间 + 样本数 + 关键词，无样本不编价 | M9-A | [x] 3 条给出中位数 + 区间 + 样本数 + 关键词，2 条因关键词太泛**主动不报价**；查价有限速 |
| 5. 两页字段完整且对应 `shop.png` / `item.png` | M9-B | [x] 真实数据渲染两页，字段核对通过 |
| 6. 连续多天我主动打开它 | 未结项 | [ ] 未起算 |

**参考库（spec/coins.md §14.13 ~ §14.15）**

| 验收项 | 对应块 | 状态 |
|---|---|---|
| 输入国家 + 年份 + 面值 → 系列信息（中文自动转英文） | M11-A | [x] 真实「英国 1900 年 1 便士」命中系列 670 等 3 条，字段齐全 |
| 输入系列号 → 全部年份单版 | M11-A | [x] 系列 670 取回 15 条单版并落库（含 1900 年发行量 31,778,100） |
| 中文→英文转换不依赖 LLM / Key | M11-A | [x] 内置 `ISSUER_CODES` + `DENOMINATION_UNITS`；未知国家明确报错 |
| 落库幂等 | M11-A | [x] 按 Numista id upsert；重抓不增行 |
| API 未提供的字段如实留空，不臆造 | M11-A | [x] 单版缺口在 spec/plan 写明；尺寸/重量/厚度/币边继承系列 |
| 目录号取全（多条逗号分隔） | M12-A | [x] `KM#790,Sp#3961` |
| `coin_series` 加 comments/mints、`coin_issues` 加 catalog_no | M12-A | [x] 老库自动 3→4 加列；实测有值 |
| `*_cn` 中文表（列数与英文表一致） | M12-A | [x] 英文表 + 中文表同时幂等落库 |
| 风控（额度自管） | M12-A | [x] 月度 1500 + 单轮 40 + 间隔 2s；CLI 显示用量 |
| 各品相估值（每年份 × G/VG/F/VF/XF/AU/UNC） | M13-A | [x] 真实系列 670 落库 15 条（1898 = `2.30 5.90 8.80 12 35 130 1200`，币种 CNY） |
| Frequency（拥有该年份/品种的用户占比） | M13-A | [x] 同上 15 条（`0.16%` ~ `45%`），编号与接口 15 个 id 一一对应 |
| 描述 / 备注 / 名称由大模型翻译（失败回退词表） | M21-A | [x] `FakeClient` 用例验「走大模型 / 失败回退 / 没 Key 只用词表」三态 |
| 存量重翻不重采 Numista | M21-A | [x] `coins-translate [--series-id N]` 读英文表 → 覆盖中文表；不重采、不加行 |
| 信息页面三条流程 + 11 条验收（§14.14） | M14 · M16 · M17 · M19 · M20 · M22 | [x] 三页真实库渲染通过；筛选 / 合并表 / 年份统计 / 宽表 / 缩略图逐条实测 |
| 图片本地缓存 + 灯箱（§14.15） | M15 | [x] 真实 CDP 下载 2 张；未缓存 404 无破图；灯箱 Playwright 实测通过 |

**历史购买品相录入（spec/coins.md §14.16）**

| 验收项 | 对应块 | 状态 |
|---|---|---|
| 去重（只列国家/年份/面值齐全的组合） | M23-A/C | [x] 真实库 1567 单 → 845 组合；去重用例覆盖 |
| 国家 / 年份 / 面值按 Numista 口径统一 | M23-C/E | [x] `英属新西兰`→`新西兰`、`1910年`→`1910`、`1便士`→`1 便士`、`一分`→`1 分`、`壹圆`→`1 圆`；年号原样 |
| 同值不同写法合并成一行 | M23-C/E | [x] 953（原始）→ 867（年份/面值）→ **845**（加国家前缀）；用例覆盖 |
| 按国家 / 年份 / 面值 / 品相查询 | M23-A/C/G | [x] 宽松包含 + 品相精确匹配；输入 `1910年` 命中 `1910` |
| 可手工新增历史订单缺失的组合 | M23-C | [x] `POST /coins/grades/add` 四项必填；新增后进列表（订单数 0） |
| **整行删除（历史错误数据）+ 二次确认** | M23-E | [x] 页内二次确认；删掉该组合全部订单（含不同写法）+ 品相记录，其余不动 |
| 品相下拉（好 / 普通 / 差） | M23-A | [x] 行内下拉 + 「未录入」；已录值回显 |
| 保存到数据库 | M23-A | [x] POST 保存 302、统一后落库；重存覆盖、选「未录入」清除 |
| 不动画像 / 推荐 | M23-A | [x] 写独立表 `coin_purchase_grades`，`coin_orders` 一行未改 |

> 当前实现取的是 spec/coins.md §14.11 的建议值（匹配阈值 1.5 / 样本下限 3 / 窗口 180 天 / 单轮上限 20 / 中位数），**匹配阈值待拿到真实推荐结果后校准**。

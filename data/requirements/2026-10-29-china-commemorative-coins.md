# 中国流通纪念币与当前流通币体系不匹配 — 需求卡

- 日期：2026-10-29 ｜ 类型：L ｜ 状态：待确认（见第 2 节）
- 结论先行：**一个收藏系列 = 一个「名称」（单枚 / 单套），主题是共享的分组字段**；画像主结构保持「按年份」，不升级为 `(年份, 名称)`。
  > 2026-10-30 修正：画像恢复为**按收藏系列汇总**（一个收藏系列一张卡），年份标签写作 `年份(名称)`（如 `1995(金丝猴)`）；
  > 主题 / 名称仍挂在成员上，品相匹配与拍品名称门槛以成员为单位。以 [spec/coins.md §14.17](../../spec/coins.md) 为准，本卡其余内容保留作历史记录。

### 1. 验收句

1. 在 `/coins/collections` 新建「中国 / 5 元 / 主题=和字书法 / 名称=和字一组」与「中国 / 10 元 / 主题=航天 / 名称=航天纪念」后，分别在 2015 录入品相「好」，`/coins/collection` 两个画像的已有年份都只含 2015，且互不串入对方的缺失年份。
2. 对同一组合 `(中国, 2015, 5元, 亚运会, 女排)` 连续保存两次品相 → 库里只有 1 行且品相为后一次值；再保存 `(亚运会, 男排)` → 新增 1 行；旧的 `(中国, 2015, 5元, '', '')` 行仍保留且不被覆盖。
3. 拍品标题「2015 中国 航天纪念币 10元」命中航天系列的精准匹配；「2015 中国 亚运会 5元 女排」命中女排系列；只有「中国 亚运会 5元」而缺名称时，不得命中任一具名系列（不判精准）。
4. 用 v26 库启动到 v27：`coin_purchase_grades` 旧行 `theme` / `coin_name` 为 `''`，唯一索引为 5 列，重复启动不报错、不重复回填。
5. 既有日本 / 南非画像与品相页行为不变：`tests/test_coins_collection.py` 与 `tests/test_coins_pages.py` 现有用例全绿，新增用例只覆盖中国纪念币。

### 2. 待确认

1. **名称列命名**：A. 新增 `coin_name`，保留 `name` 作展示名（推荐）；B. 复用 `name`。默认 A——`name` 已被「国家 · 执政者 · 面值」占用（[models.py:L413-424](file:///Users/zxw523/Documents/trae_projects/deskhub/app/models.py#L413-L424)、[collections.html:L71-88](file:///Users/zxw523/Documents/trae_projects/deskhub/app/templates/coins/collections.html#L71-L88)），复用会破坏现有页面与数据。
2. **品相加列回填**：A. 统一置 `''`，不做推断（推荐）；B. 按「国家+面值」唯一匹配自动填主题/名称。默认 A——B 在同年多主题时必错，且违背「人工维护」。
3. **主题/名称是否必填**：A. 允许为空，中国系列建卡时提示必填（推荐）；B. 数据库层强制非空。默认 A，避免阻断日本/南非旧数据与旧表单。
4. **名称匹配门槛**：A. 有 `coin_name` 时作为精准匹配硬门槛，`theme` 只加分（推荐）；B. 主题也作硬门槛。默认 A——B 会把同主题其他币误判为精准。
5. **纪念币材质放开范围**：A. 仅对**手工映射成员**按 `object_type` 放开（推荐）；B. 对所有系列放开。默认 A——B 会让探索候选池涌入大量非铜纪念币。

### 3. 改动链（按实现顺序）

| # | 文件 | 函数 / 模板 / 测试 | 改什么 |
|---|---|---|---|
| 1 | [app/models.py:L400-409](file:///Users/zxw523/Documents/trae_projects/deskhub/app/models.py#L400-L409) | SCHEMA `coin_purchase_grades` | 加 `theme TEXT NOT NULL DEFAULT ''`、`coin_name TEXT NOT NULL DEFAULT ''`；唯一索引改 `(country, era, denomination, theme, coin_name)` |
| 2 | [app/models.py:L413-424](file:///Users/zxw523/Documents/trae_projects/deskhub/app/models.py#L413-L424) | SCHEMA `coin_collection_series` | 加 `theme TEXT`、`coin_name TEXT`；`name` 仍作展示名 |
| 3 | [app/models.py:L7](file:///Users/zxw523/Documents/trae_projects/deskhub/app/models.py#L7) | `SCHEMA_VERSION` | `"26"` → `"27"` |
| 4 | `app/migrations/v27.py`（新建） | `COLUMNS` + `backfill` | 两表加列；`DROP INDEX uidx_coin_purchase_grades` 后重建 5 列唯一索引；旧行 `theme/coin_name` 置 `''` |
| 5 | [app/migrations/__init__.py:L35-120](file:///Users/zxw523/Documents/trae_projects/deskhub/app/migrations/__init__.py#L35-L120) | `COLUMN_MIGRATIONS` / `DATA_MIGRATIONS` | 登记 `("26","27")`；按现有惯例补齐 20~25 直达 27 的合并项 |
| 6 | [app/models.py:L635-643](file:///Users/zxw523/Documents/trae_projects/deskhub/app/models.py#L635-L643) | `CoinPurchaseGrade` | 加 `theme`、`coin_name` |
| 7 | [app/models.py:L954-966](file:///Users/zxw523/Documents/trae_projects/deskhub/app/models.py#L954-L966) | `CoinCollectionSeries` | 加 `theme`、`coin_name` |
| 8 | [app/repository.py:L733-779](file:///Users/zxw523/Documents/trae_projects/deskhub/app/repository.py#L733-L779) | 品相读写 4 个函数 | SELECT/INSERT/ON CONFLICT/DELETE 全部带上 5 列 |
| 9 | [app/repository.py:L824-937](file:///Users/zxw523/Documents/trae_projects/deskhub/app/repository.py#L824-L937) | `_row_to_collection_series` / `upsert_coin_collection_series` | 行映射与 upsert 加两列（`_row_to_collection_series` 定义行号未确认） |
| 10 | [app/coins/grades.py:L235-275](file:///Users/zxw523/Documents/trae_projects/deskhub/app/coins/grades.py#L235-L275) | `normalize_key` / `build_combos` | 键从 3 元组扩为 5 元组；空串保持旧合并行为 |
| 11 | [app/coins/collection.py:L167-214](file:///Users/zxw523/Documents/trae_projects/deskhub/app/coins/collection.py#L167-L214) | `_owned_years` | 匹配时若系列有主题/名称则要求品相行同值；无值系列忽略品相行的空值（旧行为不变） |
| 12 | [app/coins/collection.py:L217-257](file:///Users/zxw523/Documents/trae_projects/deskhub/app/coins/collection.py#L217-L257) | `build_profile` | 成员资格：`is_bronze_or_copper` **或**（仅手工成员且 `object_type` 含纪念/commemorative） |
| 13 | [app/coins/recommend.py:L173-175](file:///Users/zxw523/Documents/trae_projects/deskhub/app/coins/recommend.py#L173-L175) | `_keyword` | 有 `coin_name` 时拼进搜索词（国家 + 名称 + 年份 + 面值） |
| 14 | [app/coins/recommend.py:L492-504](file:///Users/zxw523/Documents/trae_projects/deskhub/app/coins/recommend.py#L492-L504) | `lot_matches` | 国家+面值+年份不变；有名称 → 名称必须命中；主题只加分不单独命中 |
| 15 | [app/routers/coins.py:L882-916](file:///Users/zxw523/Documents/trae_projects/deskhub/app/routers/coins.py#L882-L916) | `save_grades` / `_grades_context` | 表单增 `theme`、`coin_name`，按行校验与 upsert |
| 16 | [app/routers/coins.py:L1600-1633](file:///Users/zxw523/Documents/trae_projects/deskhub/app/routers/coins.py#L1600-L1633) | `add_collection` | 接收两字段；默认 `collection_key` 加入名称；支持带 `collection_key` 回填编辑 |
| 17 | [app/templates/coins/collections.html:L15-30](file:///Users/zxw523/Documents/trae_projects/deskhub/app/templates/coins/collections.html#L15-L30) | 新建表单 + 卡片 | 增主题/名称输入与展示；编辑用隐藏 `collection_key` 复用 upsert |
| 18 | [app/templates/coins/grades.html](file:///Users/zxw523/Documents/trae_projects/deskhub/app/templates/coins/grades.html) | 品相表 | 每行增主题/名称两列（行号未确认）；`_grades_results.html` 同步 |
| 19 | [app/templates/coins/collection.html:L217-280](file:///Users/zxw523/Documents/trae_projects/deskhub/app/templates/coins/collection.html#L217-L280) | 画像摘要 / 年份字典 | 标题追加主题·名称；年份表不改结构（年份仍是行键） |
| 20 | [tests/test_coins_collection.py:L248-357](file:///Users/zxw523/Documents/trae_projects/deskhub/tests/test_coins_collection.py#L248-L357) | 新增用例 | 同年同面值两系列不串、名称门槛、纪念币材质放开、旧日/南非回归 |
| 21 | [tests/test_coins_pages.py:L1565-1667](file:///Users/zxw523/Documents/trae_projects/deskhub/tests/test_coins_pages.py#L1565-L1667) | 新增用例 | 5 列 upsert/清除、旧 3 列行保留、迁移后启动 |
| 22 | [spec/coins.md:L378-L450](file:///Users/zxw523/Documents/trae_projects/deskhub/spec/coins.md#L378-L450) | §14.16 / §14.17 | 唯一键与画像口径同步（仅在实现落地后改） |

### 4. 取舍

- **粒度：系列=名称（推荐）**。`build_profile` 是以「一个收藏系列」为单位产出一份画像（[collection.py:L217-257](file:///Users/zxw523/Documents/trae_projects/deskhub/app/coins/collection.py#L217-L257)），`owned/missing/upgrade` 都挂在这份画像上；名称在系列内是常量，年份仍足以定位一枚币。若改成「系列=主题」，`name` 单值装不下多枚，只能再加一张「主题→名称」子表——正是用户已排除的新纪念币表，且 `/coins/collections` 的展开/成员/删除全部要重做。
- **画像单元不升级为 `(年份, 名称)`（推荐）**。同年多枚由「多个收藏系列共享同一 `theme`」表达，各自画像独立；升级主结构会同时改动 `CoinYearSpec`、`CoinCollectionProfile`（[models.py:L981-1025](file:///Users/zxw523/Documents/trae_projects/deskhub/app/models.py#L981-L1025)）、`year_specs` 的年份键模板与 P1/P2 去重键（[recommend.py:L280-336](file:///Users/zxw523/Documents/trae_projects/deskhub/app/coins/recommend.py#L280-L336)），收益只是把「多系列」换成「一系列多年」，代价远大于收益。
- **材质按 object_type 定向放开（推荐）**。现有 `is_bronze_or_copper` 会剔除镍包钢/黄铜/双色铜合金（[collection.py:L57-70](file:///Users/zxw523/Documents/trae_projects/deskhub/app/coins/collection.py#L57-L70)）；按「仅手工成员 + 纪念币」放开，影响面只限用户显式映射的系列，探索候选仍只收铜/青铜（[spec/coins.md:L416-L427](file:///Users/zxw523/Documents/trae_projects/deskhub/spec/coins.md#L416-L427)）。
- **主题软、名称硬（推荐）**。主题词如「亚运会」在标题里高频且跨国家，作硬门槛会大量误命中；名称（如「女排」）判别力强，作为精准匹配的必要条件，主题只参与排序加分。代价是名称未填的中国系列仍退回「国家+面值+年份」，这是可接受的降级。

### 5. 明确不做

- 不新建纪念币 / 币种表，不给 `coin_series`、`coin_series_cn` 写任何人工主题或名称，Numista 抓取覆盖逻辑不动。
- 不把 `coin_purchase_grades` 改成 `collection_id` 外键，不改订单表 `coin_orders`。
- 不升级 `CoinYearSpec` / `CoinCollectionProfile` 为 `(年份, 名称)` 结构，不改 `year_specs` 的年份键。
- 不调整日本年号换算、南非公历年回退、精制币剔除规则，不改既有文案与既有测试断言。
- 不改样式（`app/static/style.css`）、不做无关重命名/重构、不动推荐 P4/P5 的触发阈值。
- 不做主题/名称的自动从 Numista `series` / `commemorated_topic` 回填（该两列不可信）。

# 需求卡：纪念币主题/名称上移到系列详情（issue_series / issue_theme）

## 1. 验收句

1. 打开 `/coins/series/{id}`：原「所属系列」「纪念主题」两行前加「原」，其后新增「发行系列」「发行主题」，发行主题后带编辑 icon；点击 icon 可改两段文本并保存，刷新后仍为新值。
2. `/coins/info`：`object_type` 判定为纪念币的卡片，标题行末尾追加 `· {issue_theme}`；筛选区最前面出现「货币类型」下拉，选中「纪念币」后结果只剩纪念币系列，选「全部」等于现状。
3. `/coins/collections` 展开纪念币收藏系列：候选下拉每项为「Numista series + 国家 + 发行系列 + 发行主题 + 年份范围 + 面值 + 材质·尺寸(现属 XX)」，已映射系列不出现；成员表列与候选一致，原「主题 / 名称（匹配品相）」列及其保存按钮消失，操作列保留。
4. 纪念币收藏系列的年份 chip 显示 `1927(发行主题)`，品相面板年份下拉同格式；匹配按 国家 + 面值 + 年份 + 发行主题：主题相同的品相行算已有、不同主题不算，差品进升级列表。
5. 非纪念币收藏系列：候选下拉、成员表、年份 chip（无括号名称）、品相下拉与匹配结果与改动前完全一致（现有用例不改即通过）。

## 2. 待确认

1. **「是不是纪念币」按什么判**：收藏系列表无 `object_type`，只能从成员反推。选项：(a) 任一成员为纪念币即整组走新逻辑【推荐】；(b) 全部成员都是；(c) 新增收藏系列级 `object_type` 字段（要多一次迁移与录入）。
2. **issue_series / issue_theme 落哪张表、采集会不会覆盖**：选项：(a) 加在 `coin_series[_cn]`，并把两列从 Numista upsert 的更新列中排除，保留人工值【推荐】；(b) 两列照常参与 upsert（每次采集会覆盖人工编辑）；(c) 新建 `coin_series_issue_meta` 独立表（采集链不动，多一次 join）。
3. **旧字段怎么处理**：`coin_collection_series_members.theme/coin_name` 与 `coin_purchase_grades.theme/coin_name`。选项：(a) 列保留、停止读写，历史品相行 theme 为空 ⇒ 不参与主题比较，行为等同旧数据【推荐】；(b) 迁移到系列表后 DROP COLUMN；(c) 先保留，另做一次性清洗脚本（本卡不做）。

## 3. 改动链

| 文件 | 函数 / 模板块 / 测试 | 行号 |
|---|---|---|
| app/models.py | `SCHEMA_VERSION` 28→29 | 7 |
| 同上 | `coin_series` / `coin_series_cn` 加 `issue_series`、`issue_theme` | 259-320 |
| 同上 | `CoinSeries` 字段；`CoinCollectionMember.theme/coin_name`（停用标记） | 780、981-991 |
| 同上 | `CoinYearEntry.label`（括号取名改为发行主题入参） | 1031-1033 |
| app/migrations/v29.py（新建） | `COLUMNS` 两列 + `upgrade_from_28`；仿 v28 | 全文件 |
| app/migrations/__init__.py | import v29；`COLUMN_MIGRATIONS[("28","29")]`；跨版本键补 | 15-34、37-… |
| app/repository.py | `_SERIES_COLUMNS`、`_series_values`、`_row_to_coin_series` | 2383-2389、2419-2445、~2500 |
| 同上 | 新增 `update_coin_series_issue_fields`、`list_coin_series_object_types`（仿 `list_coin_series_countries`）、`search_coin_series` 加 `object_type` | 2607-2672 |
| 同上 | 成员读写 `theme/coin_name` 参数与 SQL；品相 upsert/查询 | 822-982、733-795 |
| app/coins/collection.py | `is_commemorative_object` / `is_eligible_series`（复用作展示判定） | 74-88 |
| 同上 | `_owned_years` 主题比较改取系列发行主题；`build_profile` 传参与 `year_names` | 184-242、245-310 |
| 同上 | `build_member_profiles` / `merge_profiles` 的 `commemorated_topic`、成员键 | 313-445 |
| app/routers/coins.py | `info_page` 加 `object_type` 参数与下拉 | 468-512 |
| 同上 | `series_page` 上下文 + 新增 `POST /coins/series/{id}/issue-fields` | 515-567 |
| 同上 | `_member_filters`（候选是否展示、排序） | 1027-1105 |
| 同上 | `_collection_grade_entries`（label 用发行主题，value 键改造） | 1525-1548 |
| 同上 | `collections_page` 上下文（纪念币判定、现属、列数据） | 1552-1690 |
| 同上 | `add_collection_member` / `update_collection_member`（去 theme/coin_name） | 1740-1777 |
| 同上 | `save_grades` / `add_grade` 的 theme、coin_name 表单字段 | 806-860、904-952 |
| app/templates/coins/series.html | coin-facts 块：加「原」前缀、新增两行与编辑表单 | 67-82 |
| app/templates/coins/info.html | 筛选区最前插货币类型下拉；标题行追加发行主题 | 10-40、62-65 |
| app/templates/coins/collections.html | 成员表头/行、候选 select 文案、品相面板说明 | 104-133、139-198、200-260 |
| app/templates/coins/_macros.html | 仅 `coin_library_tabs`，本需求不改 | 34-39 |
| tests/test_coins_collection.py | fixture 的 theme/coin_name、`china_series` 用例 | 164-215、380 |
| tests/test_coins_pages.py | 详情页 771/823/836、币库筛选 1030-1067、品相 theme 1603-1701；collections 用例行号待确认 | — |

**数据兼容**：新增列默认空串，历史系列显示「—」；未回填前纪念币判定仍可用，只是匹配退回「国家+面值+年份」（与现状一致）。迁移只加列、不动旧列，回滚无数据损失。采集链 `_upsert_numista_row` 需按待确认 2 的选项处理覆盖。

## 4. 取舍

- **字段落点**：加列（同表查询、改动最少，但要排除 upsert 覆盖）/ 独立表（链路隔离，多 join 与回填）。推荐加列 + upsert 排除。
- **匹配口径**：由成员 series 实时取 `issue_theme`（单一事实源、改名即时生效）/ 保存到成员成员快照（历史可追溯但会漂移）。推荐实时取。
- **展示拼装**：模板内拼接候选文案（改动集中、无新数据结构）/ repository 返回视图对象（便于测试但扩大改动面）。推荐模板内拼接。

## 5. 明确不做

- 不改 `coin_purchase_grades` / `coin_collection_series` 表结构与既有唯一索引。
- 不改采集/翻译解析字段（`coins/numista*.py`），不做主题自动回填。
- 不改非纪念币的候选与匹配逻辑、不改既有文案与断言。
- 不做样式重排、不重命名既有路由与字段、不顺手清理 `theme` 残留读写以外的代码。

status: done

# 台湾年号（民国）对齐日本年号 + 1587 备注倒序修复

## 已完成

- `app/coins/grades.py`
  - 新增 `_ROC_ERAS`（民国 1 年 = 1912）与 `_gregorian_from_roc_era`；
  - `normalize_year` 在日文年号之后接民国换算（`民国七十年` → `1981`、`民国元年` → `1912`）；
  - 新增 `is_taiwan_country` / `roc_era_label`（`1981` → `民国七十年`）/ `era_label`（日本优先、其次民国）；
  - `build_combos` 的 `era_label` 改调 `era_label`。
- `app/coins/recommend.py`：新增 `_is_taiwan`；`year_labels` 对台湾补充 `民国70年` / `民国七十年` / `民国元年`。
- 品相匹配链路无需改：`collection._parse_gregorian` 走 `normalize_year`，民国自动按公历年与系列 `gregorian_year` 对齐。
- `app/coins/translate.py`：新增 `fix_reversed_roc_legend`，把倒序钱文 `年十七國民華中` 摆正为 `中華民國七十年`。
- `app/repository.py`：`_row_to_coin_issue` 对 `comment` 调用 `fix_reversed_roc_legend`，存量数据（含 1587 的 70/1981）读取即修正。
- `app/templates/coins/grades.html`：说明与年号占位符补上民国。
- `tests/test_coins_pages.py`：`normalize_year` 民国断言、`era_label` 台湾断言。
- 已验证：`pytest tests/test_coins_pages.py -k "normalize_year or era_label or series_page_merges"` 4 passed；
  `pytest tests/test_coins_collection.py -k "japanese_years or year_labels"` 2 passed。

## 未完成

1. `tests/test_coins_translate.py` 补一条 `fix_reversed_roc_legend` 单测（可在 `test_parse_issue_comments_maps_by_id_and_skips_junk` 后插入）：
   - `"年十七國民華中"` → `"中華民國七十年"`；`"年十七國民華中 Proof"` → `"中華民國七十年 Proof"`；无匹配文本原样返回。
2. `CHANGELOG.md` 补一条（台湾民国年号按公历年、1587 备注倒序修正）。
3. 全量 `pytest -q` 一次。

## 下一步

按上面 1→2→3 顺序收口，然后 `git status`/`git diff` 自检是否只改了需求点名处。

## 约束

- 只做需求两句：台湾年号对齐日本（匹配 / 台账录入 / 换算）、1587 年份备注翻译。
- 不改系列页 `obverse_lettering` 等其他倒序字段（未被点名）。
- 不动既有断言以外的文案。

## 读取起点

- `app/coins/grades.py#L50`、`#L145`、`#L181`、`#L222`
- `app/coins/translate.py#L342`
- `app/repository.py#L2621`
- `tests/test_coins_translate.py#L101`

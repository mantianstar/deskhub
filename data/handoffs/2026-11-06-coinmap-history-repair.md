status: done
slug: coinmap-history-repair
created: 2026-11-06
task_type: S

# 币种映射历史数据修复（交接）

## 已完成（只读定位，未改代码）
- 需求：清理库里历史 `*_multi` 行（需重跑 `remap_all` 写库）；更新已落库的旧归一值（如 `1 圆`）。
- 工作区已有未提交改动（与本需求同源，勿回退）：
  - `app/coins/coinmap.py`：`match_lot` 新增多枚装 `_MULTI_COIN_RE` 直接不映射；多候选 `len(candidates)>1` 返回 `_NONE`，不再写 `*_multi`；标题只写年号时用 `era_year_from_text` 补公历年。
  - `app/coins/grades.py`：`normalize_denomination` 新增 `.replace("圆", "元")`、`1/2`→`半`；新增 `era_year_from_text`。
  - `app/models.py`：`coin_lots` 三个匹配字段注释更新。
  - `tests/test_coins_coinmap.py`、`tests/test_coins_pages.py` 同步改动。
- DB（`data/deskhub.db`）实测：
  - `coin_lots.match_method` 分布：attrs 200 / attrs_multi 56 / title 218 / title_multi 22 / none 88089 —— 共 78 行待清理。
  - `coin_purchase_grades` 491 行，`country/era/denomination` 存的就是归一后的值（现存样本 `中国|1987|1 元`）；旧行存在 `1 圆` 一类旧口径。表有唯一索引（country, era, denomination, theme, coin_name），原地改名可能撞键。
  - `coin_collection_series` 75 行，值是页面表单原样提交（创建路由 `app/routers/coins.py#L1826-L1861` 不归一化），但表单候选值来自归一后的 combo，历史行也可能含 `1 圆`。
  - `coin_orders` 是原料，明确不改（`app/coins/grades.py` 文件头注释）。
  - `coin_profile_tags` 的 `1圆` 是 LLM 原样抽取，不是归一输出，不在修复范围。
- 现有可复用入口：`app/cli.py#L344-L354` `cmd_coins_map` → `coinmap.remap_all()`；`remap_all` 只写有变化的行。
- 已确认品相落库口径（`app/routers/coins.py#L860-L871`）：`/coins/grades/add` 先调 `_normalize_grade`→`grades.normalize_key`，再把归一值 `upsert_coin_purchase_grade` 落库。**因此历史行只受当时归一规则影响**——旧 `normalize_denomination` 没有 `圆→元` / `1/2→半`，旧行才会留 `1 圆`；回填只需重算这三列，不改表单逻辑。
- 迁移注册机制：`app/migrations/__init__.py` L15-L36 导入版本模块，L222-L237 定义 `COLUMN_MIGRATIONS` / `DATA_MIGRATIONS`（如 `("30","31")`）；v31 是纯加列常量模块，v30 的 `to_beijing` 是数据迁移写法范例。新版本需在这两处同时登记。

## 未完成
- 未实现「重跑 remap_all」的落库执行（只确认了它会清 `_multi`：`_NONE` payload 为全 None）。
- 未实现旧归一值回填（`coin_purchase_grades` 及可能的 `coin_collection_series`）。
- 未跑测试、未写文档、未提交。

## 下一步
1. ~~确认回填范围~~（已完成）：品相表单落库前已归一，回填只重算历史行；`coin_collection_series` 是否一并改仍需读 `app/routers/coins.py#L1826-L1861` 定夺（该路由不归一，历史值可能是原样 `1 圆`）。
2. 实现回填：新增 `app/migrations/v32.py`（或 CLI 子命令）对 `coin_purchase_grades` 的 `country/era/denomination` 用 `grades.normalize_country/normalize_year/normalize_denomination` 重算；撞唯一键时先合并/保留后再 `UPDATE`，避免唯一键冲突丢数据。
3. 执行 `remap_all()` 写库（可走 `coins map` CLI），核对 `*_multi` 归零、`none` 增加 78。
4. 补测试（现有 `tests/test_coins_coinmap.py` 已有 remap 相关用例），跑 `.venv/bin/python -m pytest tests/test_coins_coinmap.py tests/test_coins_pages.py -q`。
5. 按需回写 `CHANGELOG.md` / `plan/coins.md`，再提交。

## 约束
- 只做被要求的；不改既有文案与测试断言；`coin_orders` 不动。
- 迁移必须幂等：已是新值则跳过；`UPDATE OR IGNORE` 后处理未更新的冲突行。
- 收尾按 RULES.md 出四行回执；token 用 `.venv/bin/python utils/token_cost/token_cost.py --round`。
- 新旧 DB 均为本地 `data/deskhub.db`，不入库。

## 读取起点
- `app/coins/coinmap.py`（`match_lot` L171 / `remap_all` L290）
- `app/coins/grades.py`（`normalize_country` L108 / `normalize_denomination` L290 / `era_year_from_text` L~161）
- `app/routers/coins.py#L740-L820`（品相组合归一与表单）
- `app/migrations/v20.py`（就地归一 + 唯一键冲突处理范例）
- `app/migrations/v27.py`（grade 索引重建）

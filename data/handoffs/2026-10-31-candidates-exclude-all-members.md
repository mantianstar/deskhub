status: done

# 交接：收藏系列候选 series 不展示已加入项

## 已完成

- 候选 series 的标题模糊筛选（服务端 `mtitle`）：`_member_filters` 增加 `title_param`，`GET /coins/collections` 与 `POST /coins/collections/members/add` 均透传；模板候选表单新增「标题」输入框；提示文案已补「标题」。
- 验证：`pytest -q tests/test_coins_pages.py tests/test_coins_collection.py` → 124 passed。

## 未完成

- 本次需求「收藏系列里的候选 series 不展示已加入收藏系列的项」尚未改动任何代码。

## 下一步

1. `app/routers/coins.py`（`collections_page`，约 1684 行）：在 `member_filters` 循环前加
   `all_member_ids = {member.numista_series_id for member in members}`，
   把循环内传给 `_member_filters` 的当前系列成员集合替换为 `all_member_ids`。
2. 同文件删除 `owner_by_series`（约 1670 行定义）与上下文里的 `"owner_by_series"`（约 1743 行）。
3. `_member_filters` docstring（约 1061 行）补一句：已加入任一收藏系列的 series 不进入候选。
4. `app/templates/coins/collections.html`（215-270 行）：删除候选 `<option>` 前的 `{% set owner = ... %}`、两处 `{% if owner %}（现属 {{ owner }}）{% endif %}`；把「已属于其他收藏系列的候选会标注「现属 X」，加入后会移入本系列。」改为「已加入收藏系列的 series 不会出现在候选中。」
5. 回归：`.venv/bin/python -m pytest -q tests/test_coins_pages.py tests/test_coins_collection.py`。

## 约束

- 只改上述链路，不动成员表、不加新样式；提示文案仅改这一条。
- 过滤必须在候选截断（50 条）之前生效；不新增排序或分页逻辑。

## 读取起点

- `app/routers/coins.py:1655-1705`
- `app/templates/coins/collections.html:215-270`

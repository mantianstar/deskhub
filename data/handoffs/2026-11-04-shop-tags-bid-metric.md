status: done

# 店铺分析页：标签列收敛 + 场均出价口径修正（2026-11-04）

- 已完成：
  - `app/static/style.css`：`.coin-risk-tags` 加 `max-width: 168px`，风险标签一行最多两个、超出换行。
  - `app/coins/shops.py`：新增 `_session_bid_stats`（出价人数按专场内 `bid_users` 去重、`bid_num` 按专场累加，再取场次中位数；缺列表的拍品按 `bid_user_num` 计入）；`_shop_metrics` / `_heat_detail` / 竞争热度评分改用新口径。
  - `app/templates/coins/shop_analysis.html`：口径说明同步。
  - `tests/test_coins_pages.py`：新增 `test_shop_session_bid_stats_dedupes_users_and_sums_lots`；`pytest -k "shops or shop_session"` 7 passed。
  - `CHANGELOG.md`：新增 v1.120。
- 未完成：无。
- 下一步：如需真机视觉确认，打开 `/coins/shops` 看标签列是否稳定两列换行。
- 约束：不改风险标签的生成逻辑，只改列宽与出价口径。
- 读取起点：`app/coins/shops.py:520`（`_session_bid_stats`）、`app/static/style.css:1709`。

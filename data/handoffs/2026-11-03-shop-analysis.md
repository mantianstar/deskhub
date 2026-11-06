status: done

# 铜币「店铺分析」页：方案确认 → 落地

## 已完成
- 已出页面方案与两版 HTML 原型：`data/prototypes/shop-analysis.html`
- 第一版含起拍溢价 / Numista 估值偏离 / 标题复现；用户反馈 5 点后已全部删除并重做
- 第二版口径：11 列榜单（成交率、真实竞价占比+覆盖率、出价人数中位、围观→出价、同标题价差、同图重挂、交付合并）+ 五维评分（成交真实度 30 / 竞争热度 20 / 价格合理 20 / 交付 20 / 样本可信 10）；详情含热度栏与脱敏出价人；无店铺外链，改站内 `/coins/analysis?shop={id}`
- 未改任何业务代码、未改库

## 未完成
- 用户已确认：入口 `/coins/shops`、旧「店铺画像」表保留并加跳转、五维权重与阈值、默认「全部时间 + 仅裸币」
- 新增确认：出价人明细不展示；新增「疑似托价（参考）」= 同一 uid 在本店出价 ≥10 场且中签率 ≤10%，此类 uid 出价占比 ≥20%，覆盖率 <30% 只展示不计分
- 「站内查看该店数据」指向现有 `/coins/analysis?shop={id}`（analysis_page 已支持 shop 参数）
- 未落地：`app/coins/shops.py`、repository 聚合 SQL、`/coins/shops` 路由、`shop_analysis.html`、CSS 并入、测试、CHANGELOG/spec 更新

## 下一步
1. 按改动链落地，冒烟测试放 `tests/test_coins_pages.py`
2. 落地时注意：bid 数据覆盖率仅约 1/21（`bid_source` 为 preview/home_filter），缺失按可得权重归一，不扣分

## 约束
- 用户明确否决：起拍溢价（大量 start_price=0）、Numista 估值偏离（覆盖低且品相不可判）、标题重复率（同名正常）、店铺外链（麦稀奇在 App/小程序内）
- 必须用起来：场次 view_count/viewer_count、拍品 bid_num/bid_user_num/bid_users
- 沿用 spec §14.19.4：成交率不含在拍、价格用中位数、样本 <5 不给结论

## 读取起点
- 原型：`data/prototypes/shop-analysis.html`
- 路由与模板：`app/routers/coins.py`（`analysis_page` 约 1249 行）、`app/templates/coins/analysis.html`
- 统计：`app/coins/analysis.py`（`build_report` / `_shops` 约 212 行）
- SQL：`app/repository.py`（`list_coin_lot_analysis_rows` 约 1721 行）
- 字段：`app/models.py`（coin_lots 150–180、coin_sessions 110–130、coin_shops 98–107、coin_lot_images 184–192）

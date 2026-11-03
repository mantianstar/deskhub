status: pending

## 已完成
- 用 httpx 直连 `https://gapi.mxiqi.com/miniprogram.home.filter` 实测（cate_id_1=11000 / cate_id_2=11070 / status=2）。
- 确认服务端 `_cond._ob` 恒为 `{"update_time": "desc"}`；17+ 个排序参数名（sort / order / order_by / sort_by / sort_type / order_field / sortBy / _ob dict 等）全部被丢弃，不出现在 `_cond`。
- 确认没有可自定义的成交时间范围：传 `end_time` / `auction_end_time` 会触发服务端写死的 `_range: {auction_end_time: {gte: now/d, lte: now+10d}}`；`_range` 直传被丢弃。
- 白名单里唯一被接受的新键是 `auction_mode` 与 `name_user`，都不改变排序。
- 站点「最近成交」未找到独立 action：`miniprogram.auction.soldList` / `home.sold` / `home.recent` / `sold.list` / `auction.recentSold` / `auction.sold` 均返回 302 `method does not exists`。

- 另测两支首页接口：`home.getV3` = 首页聚合（swiper / topics / auction_items / auction_type=today / auctions 14 场 / ct_* 角标），`without_items=1` 必须放 query 才生效（去掉 auction_items，260KB→228KB）；`home.listItems` = 首页拍品流分页（pageSize 10、totalSize 封顶 10000），只认 `page`，topic_id / auction_type / cate_id_1/2 / auction_id / status 全被忽略，返回 status=1 的在拍条目。

## 未完成
- 「最近成交」tab 的真实请求未抓到（小程序包未取），只能推断为同一支 filter + status=2。
- `home.listItems` 的参数名可能不叫 topic_id / section，未逐个试（无 `_cond` 回显，只能靠结果集比对）。
- `cate_id_3` 是否被白名单接受未测（条目里有 `cate_ids` 字段）。
- `auction_mode` 取值对结果集的影响未测。

## 下一步
- 若要补 398 缺口：先测 `cate_id_3` / `auction_mode` 是否收窄结果集、`totalSize` 是否随之下降，再决定是否改 `filter_scan` 的页分配。
- 若坚持按截拍时间全覆盖，需要另找入口（抓包小程序或 H5），不能靠 home.filter 排序。

## 约束
- 只读探查，未改仓库代码；`home.filter` 每页固定 21 条、totalSize 封顶 10000。
- 探测脚本均为 shell 内 `python -c`，未落盘。

## 读取起点
- app/coins/gapi.py L647-L678（search_sold / search_filter_items）
- app/coins/filter_scan.py L291-L299（_page_all_stale）、L376-L390（search_params 构造）

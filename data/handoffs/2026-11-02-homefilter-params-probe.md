status: pending
slug: homefilter-params-probe
created: 2026-11-02

- 已完成：
  - 实测 `miniprogram.home.filter` 对 `auction_id` / `org_id` 参数静默忽略（21 条里本场 0 条，返回默认全站流）。
  - 实测 `keywords="沼泽如烟"` 逐字模糊匹配场次名失败（返回「戒烟？」等，库内命中 0/21，totalSize 封顶 10000）。
  - 实测 `auction.get {auction_id: 331881}` 一次返回 537 条，与库内 537 行完全对上；但拍品列表只有 1/537 带 `bid_num`，`num` 字段恒为 1（非出价次数），无法回填热度字段。
  - 确认报告里的「1909」是展示序号，真实 `auction_id=331881`、`org_id=2256`。
  - 尝试抓 `https://www.mxiqi.com/` 取前端 JS：只返回 2.7KB 风控壳，无 JS 资源。
- 未完成：
  - 未拿到小程序前端 bundle，`search_params` 的完整构造仍未确认。
  - 未确认 filter 是否还有 `auction_id` / `org_id` 之外的隐藏字段（未知字段被静默忽略，无法用「传了没差异」反证）。
- 下一步：
  - 抓包微信小程序（mitmproxy / Charles）取 `miniprogram.home.filter` 原始请求体，看搜索页 / 专场页实际传了哪些 `search_params`。
  - 或取 `.wxapkg` 反编译后检索 `home.filter` 的入参拼装。
  - 若两者都不可行，按「filter 只能按 cate 切」定论，把缺口回填策略写成深扫游标推进，不再做场次定向。
- 约束：
  - 不改业务代码；探测请求不计入 `record_api_call`，但会消耗站点风控额度。
  - 单段会话累计输入已超 1.2M，续跑必须换新会话。
- 读取起点：
  - `app/coins/filter_scan.py` L328-L400（`_scan` 里 `search_params` 构造）、L291-L299（`_page_all_stale`）。
  - `app/coins/gapi.py` L647-L678（`search_sold` / `search_filter_items`）。
  - `plan/coins.md` L889-L898（分类参数实测表）、L781（`org_id` / `owner_id` / `org` 被忽略）。
  - 旧交接：`data/handoffs/2026-11-02-homefilter-sort-probe.md`（仍为 pending）。

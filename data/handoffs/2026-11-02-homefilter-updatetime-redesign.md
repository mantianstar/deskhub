status: done
slug: homefilter-updatetime-redesign
created: 2026-11-02

- 已完成：
  - 实测确认 `miniprogram.home.filter` 按 `update_time`（本地时间，Asia/Shanghai）降序，不是按成交时间；`update_time` 是上架/最后更新时间，早于 `closed_at` 数小时到数月。
  - 11050 取样 6 页（1/2/21/60/120/183）：p1 update 10-30~11-01、p2 10-29~10-30、p21 10-22、p60 10-11、p120 09-04、p183 07-28；p1/p2/p21 命中库内 21/21，p60 以后 0/21。
  - 1101 成交覆盖核对：11050 共 367 条，`bid_source IS NULL` 327 条（从未扫到），`home_filter` 35 条（全部已覆盖）；11070 已扫 716 条全覆盖、未扫 351 条。结论：缺口是真实搜索空洞，不是 bid_num 字段缺失。
  - 已实现并测试通过（12 passed）的三段配额版本：顶部按近 3 日成交量估算、昨日回补段、深扫余量二次回收、按类额度日志。
- 未完成：
  - 未按新排序语义重构：昨日回补段（`BACKFILL_*`）在此排序下第 21 页即判停，等于空转；`DEEP_LOOKBACK_DAYS=7` 实际是「只扫 7 天内上架」，与目标无关。
- 下一步：
  - 删除 backfill 段与 `BACKFILL_*` 常量；顶部页数改回固定 20 页 + `TOP_REFRESH_HOURS` 保活。
  - `deep_floor` 取消时间停扫，改为扫到空页 / `FULL_MAX_PAGES` 底；只对 `count_uncovered_sold_by_category() > 0` 的类深扫。
  - 预估成本：需深扫的 6 类 × 约 476 页 ≈ 2856 页，按 900/天（扣顶部）约 3-4 天一轮。
- 约束：
  - 不改 `outcome` / `final_price` / `closed_at`；探测请求不写 `record_api_call`，但消耗站点风控额度。
  - 顶部/断点都依赖「排序稳定为 update_time 降序」；若站点改排序，`next_page` 需整体重置。
- 读取起点：
  - `app/coins/filter_scan.py` L39-L53（常量）、L427-L473（配额组装）、L489-L583（三段循环与游标）。
  - `app/repository.py` L2828-L2859（缺口 / 近 N 日成交 / 进度读取）。
  - 本轮探针脚本：`/tmp/probe_update_time.py`。

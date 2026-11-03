status: done

# 2026-11-03 专场围观人数（viewer_count）

- 已完成：`auction.get` 的 `auction_user_total` → `SessionCard.viewer_count` → `CoinSession.viewer_count` → `coin_sessions.viewer_count`（v31 迁移）→ `CoinSessionCard` / `CoinSessionDetail` → sessions.html / session.html 展示；`set_coin_session_status` 用 COALESCE 回写；全量测试 526 passed / 1 skipped。
- 未完成：历史场次尚未回采，旧数据 `viewer_count` 为 NULL（模板按条件不渲染）。
- 下一步：如需补齐历史数据，重跑 detail 采集即可（已结束场次仍返回该字段）。
- 约束：人数是累计去重 UV，与 `view_count`（次数）并列展示，不替换原字段。
- 读取起点：app/coins/gapi.py `detail_from_payload`、app/migrations/v31.py。

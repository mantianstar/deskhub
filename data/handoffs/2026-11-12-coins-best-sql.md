status: done

- 已完成：定位 `match_today_lots` 依赖的取数函数位置（app/repository.py L1410 `list_coin_today_lots`、L3200 `list_coin_series`；app/coins/recommend.py L118 `denomination_variants`），尚未读取函数体，未产出 SQL。
- 未完成：把 `match_today_lots` 的匹配逻辑（国家 + 面值变体 + 年份区间，多候选取 span 最小）转成用户可直接在 SQLite 里执行的一条查询。
- 下一步：读 repository.py L1410-1465、L3200-3246，recommend.py L118-160，确认真实表名 / 字段与「今日」「11070 类」的过滤写法；注意 best.py 的 `_squash`（去空白）、年份正则 `(1[5-9]\d{2}|20\d{2})`、BEST_CATEGORY_PREFIX=11070、`config.app.tz` 的今日边界。
- 约束：只读问答，不改仓库；交付物是 SQL 文本（可附一段 CTE 版说明），不要顺手改代码。
- 读取起点：app/coins/best.py L36-100；app/repository.py L1410、L3200；app/coins/recommend.py L118

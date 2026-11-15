status: done

# 交接：铜币每日任务分段容错 + 断点续跑 + 评级独立（已完成）

- 已完成：需求卡 data/requirements/2026-11-15-coins-daily-resumable-stages.md 全部落地（models v39 两张表、db 前向升级、repository 6 个函数、pipeline grade_only、scheduler 四段独立 _run_stage + resume_incomplete、main lifespan 续跑、switches.COINS_GRADE、tasks.run_coins_grade、CHANGELOG v1.160）；定向测试 tests/test_scheduler.py + test_repository.py + test_control.py 全部通过。
- 未完成：无。
- 下一步：无；若要提交，注意工作区还混有 v1.159（collection/session 页面）未提交改动，需分开提交。
- 约束：未改 daily_pipeline 语义、未改 /control 模板与文案、未动限速与 MISFIRE_GRACE_SECONDS、四段仍串行。
- 读取起点：app/scheduler.py（_DAILY_COINS_STAGES / _run_stage / resume_incomplete）。

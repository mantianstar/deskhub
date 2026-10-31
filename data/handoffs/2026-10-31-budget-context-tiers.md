status: done

# 2026-10-31 预算提示分档重构 + 交接文档落地

- 已完成：ctx 阈值改 60k/80k/100k 并差异化文案（一行 / 写文件 / 模板）；停线全篇唯一；
  输入三档加交接路径；800k 删除失真「可省」估算；「还能发 N 条」改二次解，hook 与
  token_cost 同口径；guard_reset 首条消息注入 pending 交接提问；docs / RULES / CHANGELOG / 测试更新。
- 未完成：全量 `pytest -q` 仍有 5 个**与本任务无关**的既有失败（工作区先前未提交改动导致）：
  `test_coins_categories.py` 3 个（分类 schema / v20 升级 / v21 幂等）、
  `test_snippet_whole_file_read_cap_matches_hook`（project-rules 写 150、guard_read 用 120）、
  `test_read_state_resets_on_user_prompt_and_content_change`（guard_read 已改提示、测试仍断言 deny）。
- 下一步：确认是否修这 5 个既有失败；本任务的 hook / rules / docs 用例 51 个全通过。
- 约束：不改 guard_read / guard_search / guard_pytest 判据；不动 token_cost 的累计输入与成本口径。
- 读取起点：`.trae/hooks/guard_budget.py` 常量 33-44、`estimate_left_requests` 81-94、
  `tier_text` 138-183、`ctx_warn_text` 186-206；`.trae/hooks/guard_reset.py` 全文。

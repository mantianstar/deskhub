"""UserPromptSubmit 门禁：新用户消息 = 新任务边界，清空本会话的已读 / 已搜 / 已跑命令。

**不清上下文**：`created_ts`（会话起点）保留——用户发第二句话时历史并没有归零，
跟着重置会让「累计输入」永远打不到 800k / 1.2M，正好漏掉最贵的场景。
只把已读 / 已搜 / 已跑命令归零；回执里的「轮 / 请求」由 `guard_budget` 从流水锚点算，不落状态。

不阻断任何操作，也不输出 stdout；拿不到 session_id 或状态文件不可用时静默跳过。
"""

from __future__ import annotations

import json
import sys
import time

import _state


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return

    with _state.locked():
        state = _state.load()
        slot = _state.bucket(state, session_id)
        slot["reads"] = {}
        slot["prints"] = {}
        slot["searches"] = []
        slot["cmds"] = []
        slot["task_start_ts"] = int(time.time())
        slot["last_ctx_warn"] = 0
        slot.setdefault("created_ts", int(time.time()))
        _state.save(state)


if __name__ == "__main__":
    main()

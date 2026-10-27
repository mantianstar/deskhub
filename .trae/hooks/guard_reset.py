"""UserPromptSubmit 门禁：新用户消息 = 新任务边界，清空本会话的已读 / 已搜 / 已跑命令。

不阻断任何操作，也不输出 stdout；拿不到 session_id 或状态文件不可用时静默跳过。
"""

from __future__ import annotations

import json
import sys

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
        sessions = state.get("sessions") or {}
        if session_id in sessions:
            sessions.pop(session_id, None)
            _state.save(state)


if __name__ == "__main__":
    main()

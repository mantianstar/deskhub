"""Hook 会话状态：按 session_id 存「已读区间」与「已跑命令」，供门禁去重。

放在 data/hook_state.json；读坏 / 写不进一律按空状态处理，绝不让门禁脚本崩。
注意：并发工具调用（同一轮多个 Read）存在读-改-写竞争，可能丢一条登记，
这里刻意不做文件锁——丢一条只会少拦一次，不会误拦。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("DESKHUB_HOOK_STATE") or REPO / "data" / "hook_state.json")
MAX_SESSIONS = 5


def load() -> dict:
    try:
        with STATE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(tmp, STATE)
    except OSError:
        pass


def bucket(state: dict, session_id: str) -> dict:
    """取当前会话的槽，顺手淘汰最老的会话，避免状态文件无限增长。"""
    sessions = state.setdefault("sessions", {})
    slot = sessions.setdefault(session_id, {"ts": 0, "reads": {}, "cmds": []})
    slot["ts"] = int(time.time())
    if len(sessions) > MAX_SESSIONS:
        stale = sorted(sessions, key=lambda sid: sessions[sid].get("ts", 0))
        for sid in stale[: len(sessions) - MAX_SESSIONS]:
            sessions.pop(sid, None)
    return slot

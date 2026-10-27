"""Hook 会话状态：按 session_id 存「已读区间」「已跑命令」「已搜关键词」，供门禁去重。

放在 data/hook_state.json；读坏 / 写不进一律按空状态处理，绝不让门禁脚本崩。
同一轮多个工具调用会并发写这个文件，读-改-写必须串行：用 `.lock` 上的 flock 包住
`load → 改 → save`，拿不到锁（默认 50ms）就 fail-open——少拦一次好过卡住工具调用。
"""

from __future__ import annotations

import contextlib
import fcntl
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


@contextlib.contextmanager
def locked(timeout: float = 0.05):
    """串行化 `load → 改 → save`，避免并发工具调用互相覆盖登记。

    拿不到锁就照常往下走（fail-open）：门禁漏拦一次，比让工具调用卡住好。
    """
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        handle = STATE.with_suffix(".lock").open("a")
    except OSError:
        yield
        return

    held = False
    try:
        deadline = time.monotonic() + timeout
        while not held:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
        yield
    finally:
        if held:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


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

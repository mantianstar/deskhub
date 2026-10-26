"""PreToolUse(RunCommand) 门禁：同一条 pytest 不在同一会话重跑（RULES.md §二.4）。

只认「命令位」上的 pytest（`pytest ...`、`python -m pytest ...`），
`grep pytest` 这类查词不算测试，直接放行。
确需重跑时在命令末尾加 `# rerun`：命令串变了，哈希自然不同，不需要白名单。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import _state

REPO = Path(__file__).resolve().parents[2]
AUDIT = Path(os.environ.get("DESKHUB_HOOK_AUDIT") or REPO / "data" / "hook_audit.log")
MAX_CMDS = 50
RERUN_MARK = "# rerun"
TEST_RE = re.compile(r"(?:^|[;&|]\s*)(?:(?:\S*/)?python[\d.]*\s+-m\s+)?pytest\b")


def audit(command: str, decision: str, detail: str = "") -> None:
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "hook": "PreToolUse(RunCommand)",
                        "object": command[:120],
                        "decision": decision,
                        "detail": detail,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                    "additionalContext": reason,
                }
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not TEST_RE.search(command):
        return

    norm = " ".join(command.split())
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    if RERUN_MARK in norm:
        audit(norm, "allow", "rerun")
        return

    state = _state.load()
    slot = _state.bucket(state, session_id)
    cmds = slot.setdefault("cmds", [])
    key = hashlib.sha1(norm.encode("utf-8")).hexdigest()

    if key in cmds:
        reason = (
            "这条 pytest 已在本会话执行过（RULES.md §二.4：同一事实只验证一次）。"
            "修完代码确需重跑，请在命令末尾追加 `# rerun` 再执行；"
            "换测试范围不算重跑，直接跑即可。"
        )
        audit(norm, "deny", "repeat")
        deny(reason)
        return

    cmds.append(key)
    slot["cmds"] = cmds[-MAX_CMDS:]
    _state.save(state)
    audit(norm, "allow", "record")


if __name__ == "__main__":
    main()

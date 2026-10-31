"""UserPromptSubmit 门禁：新用户消息 = 新任务边界，清空本会话的已读 / 已搜 / 已跑命令。

**不清上下文**：`created_ts`（会话起点）保留——用户发第二句话时历史并没有归零，
跟着重置会让「累计输入」永远打不到 800k / 1.2M，正好漏掉最贵的场景。
只把已读 / 已搜 / 已跑命令归零；回执里的「轮 / 请求」由 `guard_budget` 从流水锚点算，不落状态。

会话首条消息另做一件事：扫 `data/handoffs/*.md`，首行 pending 的最新一个注入提问式一行，
让模型先问用户是否续跑。**边界**：hook 不能新建会话，能自动的只有「恢复」；
新建仍要用户点一次。非首条、无文件、读不到一律不注入（fail-open）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import _state

REPO = Path(__file__).resolve().parents[2]
HANDOFF_DIR = Path(os.environ.get("DESKHUB_HANDOFF_DIR") or REPO / "data" / "handoffs")
PENDING = "status: pending"


def pending_handoff() -> Path | None:
    """首行 pending 的最新交接文件；目录不存在、单个文件读不到都跳过。"""
    latest: tuple[float, Path] | None = None
    for path in HANDOFF_DIR.glob("*.md"):
        try:
            with path.open(encoding="utf-8") as fh:
                if not fh.readline().strip().startswith(PENDING):
                    continue
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if latest is None or mtime > latest[0]:
            latest = (mtime, path)
    return latest[1] if latest else None


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
        first_message = "created_ts" not in slot
        slot["reads"] = {}
        slot["prints"] = {}
        slot["read_counts"] = {}
        slot["searches"] = []
        slot["cmds"] = []
        slot["task_start_ts"] = int(time.time())
        slot["last_ctx_warn"] = 0
        slot.setdefault("created_ts", int(time.time()))
        _state.save(state)

    if not first_message:
        return
    path = pending_handoff()
    if path is None:
        return
    try:
        shown = path.relative_to(REPO)
    except ValueError:
        shown = path
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        f"发现待确认交接：{shown}。先问用户是否继续该任务，用户确认后再读文件执行；"
                        "用户要做别的事就忽略，不要主动执行。执行后把首行改成 status: done。"
                    ),
                }
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

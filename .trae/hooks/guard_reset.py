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
ROSTER_MAX = 6  # 已读摘要最多列 6 个文件：多一行就多一份每轮重发的成本


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


def read_roster(slot: dict) -> str:
    """把本段已读压成一行：换任务时清空去重表（允许重读），但先告诉模型读过什么。"""
    reads = slot.get("reads")
    if not isinstance(reads, dict) or not reads:
        return ""
    items: list[str] = []
    for path, spans in list(reads.items())[-ROSTER_MAX:]:
        real = Path(str(path))
        try:
            shown = str(real.relative_to(REPO)) if real.is_absolute() else str(real)
        except ValueError:
            shown = real.name
        ranges: list[str] = []
        for span in (spans or [])[:2]:
            try:
                ranges.append(f"{int(span[0])}-{int(span[1])}")
            except (TypeError, ValueError, IndexError):
                continue
        items.append(shown + (":" + ",".join(ranges) if ranges else ""))
    rest = len(reads) - len(items)
    tail = f"；另有 {rest} 个" if rest > 0 else ""
    return f"本段已读（不必重读，改前只补未读区间）：{'、'.join(items)}{tail}"


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
        roster = read_roster(slot)
        slot["reads"] = {}
        slot["prints"] = {}
        slot["read_counts"] = {}
        slot["searches"] = []
        slot["cmds"] = []
        slot["task_start_ts"] = int(time.time())
        slot["last_ctx_warn"] = 0
        slot.setdefault("created_ts", int(time.time()))
        _state.save(state)

    parts: list[str] = []
    if first_message:
        path = pending_handoff()
        if path is not None:
            try:
                shown = path.relative_to(REPO)
            except ValueError:
                shown = path
            parts.append(
                f"发现待确认交接：{shown}。先问用户是否继续该任务，用户确认后再读文件执行；"
                "用户要做别的事就忽略，不要主动执行。执行后把首行改成 status: done。"
            )
    if roster:
        parts.append(roster)
    if not parts:
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n".join(parts),
                }
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

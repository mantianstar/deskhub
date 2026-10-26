"""PreToolUse(Read) 门禁：大文件必须分段读（RULES.md §四）。

文件超过 200 行且没带 limit（或剩余部分超过 200 行）时拒绝本次 Read，
并把「改用 offset/limit」的理由回传给模型；小文件、二进制、参数缺失一律放行。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import _state

MAX_LINES = 200

REPO = Path(__file__).resolve().parents[2]
AUDIT = Path(os.environ.get("DESKHUB_HOOK_AUDIT") or REPO / "data" / "hook_audit.log")


def audit(obj: str, decision: str, detail: str = "") -> None:
    """记一条审计：Hook 是否真的被调用、放行还是拒绝。写不进去不影响判定。"""
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "hook": "PreToolUse(Read)",
                        "object": obj,
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


def line_count(path: str) -> int | None:
    """返回文本文件行数；二进制文件或读不到时返回 None。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
            if b"\x00" in head:
                return None
            count = head.count(b"\n")
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    return count
                count += chunk.count(b"\n")
    except OSError:
        return None


def merge_spans(spans: list) -> list:
    """合并已读区间；末端为 None 表示读到文件末尾，吞掉其后所有区间。"""
    out: list = []
    for start, end in sorted(spans, key=lambda s: s[0]):
        if out:
            prev_start, prev_end = out[-1]
            if prev_end is None:
                continue
            if start <= prev_end:
                out[-1] = [prev_start, None if end is None else max(prev_end, end)]
                continue
        out.append([start, end])
    return out


def remember(payload: dict, path: str, start: int, end: int | None) -> str | None:
    """窗口已被本会话读过就返回拒绝理由；否则登记并返回 None（RULES.md §二「同一文件多次读」）。"""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None

    state = _state.load()
    slot = _state.bucket(state, session_id)
    reads = slot.setdefault("reads", {})
    spans = reads.get(path) or []

    for prev_start, prev_end in spans:
        if prev_start <= start and (prev_end is None or end <= prev_end):
            tail = "文件末尾" if prev_end is None else f"{prev_end} 行"
            return (
                f"{path} 的 {prev_start}–{tail} 本会话已读过"
                "（RULES.md §二：同一文件不重复读）。"
                "没读到的部分换 offset 取；已读过的段落不要再整段重读。"
            )

    reads[path] = merge_spans(spans + [[start, end]])
    _state.save(state)
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        audit("<no file_path>", "allow", "skip")
        return

    limit = tool_input.get("limit")
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None

    if limit is not None and limit > MAX_LINES:
        audit(path, "deny", f"limit={limit}>{MAX_LINES}")
        deny(f"Read 的 limit={limit} 超过单段 {MAX_LINES} 行上限（RULES.md §四），请改成分段读。")
        return
    offset = tool_input.get("offset") or 0
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0

    if limit is not None:
        reason = remember(payload, path, offset, offset + limit)
        if reason:
            audit(path, "deny", f"repeat limit={limit} offset={offset}")
            deny(reason)
            return
        audit(path, "allow", f"limit={limit}")
        return

    total = line_count(path)
    if total is None or total <= MAX_LINES:
        reason = remember(payload, path, offset, None)
        if reason:
            audit(path, "deny", f"repeat total={total} offset={offset}")
            deny(reason)
            return
        audit(path, "allow", f"total={total}")
        return

    remaining = total - offset
    if remaining <= MAX_LINES:
        reason = remember(payload, path, offset, None)
        if reason:
            audit(path, "deny", f"repeat offset={offset} remaining={remaining}")
            deny(reason)
            return
        audit(path, "allow", f"offset={offset} remaining={remaining}")
        return

    audit(path, "deny", f"total={total} offset={offset}")
    deny(
        f"{path} 共 {total} 行，整份读会刷屏（RULES.md §四：单轮 ≤200 行）。"
        "请带 offset/limit 分段读，单段 ≤120 行；不要原样重试。"
    )


if __name__ == "__main__":
    main()

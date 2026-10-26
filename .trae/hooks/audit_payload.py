"""临时审计 Hook：把 PreToolUse 的真实 payload 形状落盘。

只在验证期启用（hooks.json 里 matcher="*"），用来确认真实 tool_name 取值与字段结构。
不做任何判定：不输出 stdout、永远 exit 0，避免影响被审计的工具调用。
字符串 / 数组 / 字典一律截断，防止 Write、Edit 的大段内容灌进日志。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get("DESKHUB_HOOK_SAMPLES") or REPO / "data" / "hook_payload_samples.jsonl")
MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 3


def shrink(value, depth: int = 0):
    """把任意值压成定长摘要：字符串留 100 字，字典留 15 键，数组留 3 项。"""
    if isinstance(value, str):
        return value if len(value) <= 100 else f"{value[:100]}…(共{len(value)}字)"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= MAX_DEPTH:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        out: dict = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= 15:
                out["…"] = f"共{len(value)}个键"
                break
            out[str(k)] = shrink(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = [shrink(v, depth + 1) for v in list(value)[:3]]
        if len(value) > 3:
            items.append(f"…共{len(value)}项")
        return items
    return f"<{type(value).__name__}>"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return

    try:
        if OUT.exists() and OUT.stat().st_size > MAX_BYTES:
            return
    except OSError:
        pass

    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": payload.get("hook_event_name"),
        "tool_name": payload.get("tool_name"),
        "llm_tool_name": payload.get("llm_tool_name"),
        "tool_use_id": payload.get("tool_use_id"),
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "workspace_roots": payload.get("workspace_roots"),
        "top_keys": sorted(payload.keys()),
        "tool_input": shrink(payload.get("tool_input")),
    }
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()

"""PostToolUse(Write|Edit|DeleteFile) 审计：记录写入动作与体量，不阻断、不输出 stdout。

Q/D 的「不改文件」不做硬拦（避免误伤 S/L），越界靠审计日志与本轮回执暴露。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUDIT = Path(os.environ.get("DESKHUB_HOOK_AUDIT") or REPO / "data" / "hook_audit.log")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return

    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    size = 0
    for key in ("content", "new_string"):
        value = tool_input.get(key)
        if isinstance(value, str):
            size = len(value.encode("utf-8"))

    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "hook": f"PostToolUse({payload.get('tool_name')})",
                        "object": str(path)[:120],
                        "decision": "audit",
                        "detail": f"bytes={size}",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


if __name__ == "__main__":
    main()

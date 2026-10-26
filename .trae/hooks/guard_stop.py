"""Stop 门禁：回复缺「回执：」或 token 成本清单就要求补（RULES.md §零.3）。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUDIT = Path(os.environ.get("DESKHUB_HOOK_AUDIT") or REPO / "data" / "hook_audit.log")


def audit(decision: str, detail: str = "") -> None:
    """记一条审计：Stop 门禁是否真的被调用、放行还是阻断。写不进去不影响判定。"""
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "hook": "Stop",
                        "object": "-",
                        "decision": decision,
                        "detail": detail,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    message = payload.get("last_assistant_message") or ""
    missing: list[str] = []
    if "回执：" not in message and "回执:" not in message:
        missing.append("「回执：」三行")
    if not ("token" in message.lower() and "成本" in message):
        missing.append("token 成本清单")

    if not missing:
        audit("allow", "complete")
        return

    audit("block", "、".join(missing))
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    "按 RULES.md §零.3，本条回复缺 "
                    + "、".join(missing)
                    + "。请在末尾补「回执：」三行（实际轮数 / 上限、搜索与读取次数、越界项）"
                    + "和 token 成本清单（.venv/bin/python utils/token_cost/token_cost.py --round）后再结束。"
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

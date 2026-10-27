"""Stop 门禁：回复缺「回执：」三段结构或 token 成本清单就要求补（RULES.md §零.3）。"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUDIT = Path(os.environ.get("DESKHUB_HOOK_AUDIT") or REPO / "data" / "hook_audit.log")

RECEIPT_RE = re.compile(r"回执[：:]")
# 「轮数」也只认固定行（RULES.md §零.3），与成本行同口径：
#   - 轮数：3 / 10
# 不再从全文里猜「N / M」或「N 轮（/M）」——那样正文里任意比分都会被当回执。
ROUND_LINE_RE = re.compile(
    r"^[ \t]*(?:(?:[-*]|\d+[.)])[ \t]*)?"  # 项目符号或序号可选
    r"轮数[：:][ \t]*\d+[ \t]*[/／][ \t]*\d+[^\n]*$",
    re.M,
)
# 「成本清单」只认固定行（docs/token-cost.md §3），不再猜「成本 + 数字」：
#   - 成本：输入 3,789,650 + 输出 54,385 = 合计 3,844,035
#   - 成本：未计量（以平台账单为准）
COST_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]*)?成本[：:][ \t]*"
    r"(?:输入[ \t]*[\d,]+[ \t]*\+[ \t]*输出[ \t]*[\d,]+[ \t]*[=＝][ \t]*合计[ \t]*[\d,]+|未计量)"
    r"[^\n]*$",
    re.M,
)


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


def missing_parts(message: str) -> list[str]:
    """三段结构校验：回执标题 → 固定轮数行 → 固定成本行。"""
    match = RECEIPT_RE.search(message)
    if not match:
        return ["「回执：」三行"]
    tail = message[match.end() :]
    missing: list[str] = []
    if not ROUND_LINE_RE.search(tail):
        missing.append("轮数（固定一行：`- 轮数：实际 / 上限`，如 `- 轮数：3 / 10`）")
    if not COST_LINE_RE.search(tail):
        missing.append("token 成本清单（固定一行：`- 成本：输入 X + 输出 Y = 合计 Z`，无新增写 `- 成本：未计量`）")
    return missing


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    message = payload.get("last_assistant_message") or ""
    missing = missing_parts(message)

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
                    + "。请在末尾补「回执：」三行（`- 轮数：实际 / 上限`、搜索与读取次数、越界项）"
                    + "和 token 成本清单（.venv/bin/python utils/token_cost/token_cost.py --round）后再结束。"
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

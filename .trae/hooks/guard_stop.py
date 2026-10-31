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

RECEIPT_RE = re.compile(r"^[ \t]*(?:[-*][ \t]*)?\**回执[：:]\**[ \t]*$", re.M)
# 回执固定四行（RULES.md §零.3）：首行只有「回执：」，下面三行项目符号：
#   回执：
#   - 轮数：3 ｜ 请求：4 ｜ 上下文：20,447→44,057
#   - 搜索/读取：Grep×3 · 读 364 行 ｜ 输出峰：6KB ｜ 越界：无
#   - 成本：输入 342,213 + 输出 8,586 = 合计 350,799
# 轮数按批次口径（同批并行算 1 次），不带「/ 上限」：`/数字` 只在旧写法里出现，按可选处理。
ROUND_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]*)?轮数[：:][ \t]*\d+(?:[ \t]*[/／][ \t]*\d+)?[ \t]*[｜|]",
    re.M,
)
# 余额：轮数行末尾的「还能发 N 条」，由 `--round` 锚点行产出，模型只照抄（生成比感知强）。
LEFT_LINE_RE = re.compile(r"还能发[ \t]*\d+[ \t]*条")
# 「成本清单」只认固定行（docs/token-cost.md §3），不再猜「成本 + 数字」：
#   - 成本：输入 3,789,650 + 输出 54,385 = 合计 3,844,035
#   - 成本：未计量（以平台账单为准）
# 数字允许 k / M / 万 后缀（如 342k、1.2M、34万），与 --round 输出的缩写口径一致。
_NUM = r"[\d,]+(?:\.\d+)?[kKmM万]?"
COST_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]*)?成本[：:][ \t]*"
    r"(?:输入[ \t]*" + _NUM + r"[ \t]*\+[ \t]*输出[ \t]*" + _NUM + r"[ \t]*[=＝][ \t]*合计[ \t]*" + _NUM + r"|未计量)"
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
    """四行结构校验：首行「回执：」→ 轮数行 → 成本行。"""
    match = RECEIPT_RE.search(message)
    if not match:
        return ["整段回执（行首「回执：」没有输出）"]
    tail = message[match.end() :]
    missing: list[str] = []
    if not ROUND_LINE_RE.search(tail):
        missing.append("轮数行（`- 轮数：3 ｜ 请求：4 ｜ 上下文：20,447→44,057`，数字换成实际值）")
    if not LEFT_LINE_RE.search(tail):
        missing.append("余额（轮数行末尾缺 `｜ 还能发 N 条`，照抄 `--round` 锚点行）")
    if not COST_LINE_RE.search(tail):
        missing.append("token 成本清单（固定一行，照抄 `--round` 末行，如 `- 成本：输入 213,171 + 输出 7,225 = 合计 220,396`；无新增写 `- 成本：未计量`）")
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
                    + "。补齐四行（尖括号换实际数字，别照抄占位符）：回执：\\n"
                    + "- 轮数：<轮数> ｜ 请求：<请求数> ｜ 上下文：<起始>→<结束> ｜ 还能发 <N> 条\\n"
                    + "- 搜索/读取：<明细> ｜ 输出峰：<KB> ｜ 越界：无\\n"
                    + "- 成本：输入 <输入> + 输出 <输出> = 合计 <合计>"
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

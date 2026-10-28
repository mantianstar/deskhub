"""PreToolUse 计量：批次轮数 + 累计输入档位 + 上下文封顶提示（RULES.md §二 / §零.3）。

只注入文字，**不做任何 deny**：一次 deny 约等于一整个上下文，是注入的 40 倍。
读不到 usage 流水一律 fail-open，能算多少算多少。

口径：
- 轮 = 本轮（`guard_reset` 记的 `task_start_ts` 之后）已落盘的请求数 + 在飞这一条：
  一条请求里的并行工具调用天然同轮；不用「间隔 ≤3 秒」猜批次——快串行会被并成一批，
  慢串行又被拆成多轮，两个方向都错；
- 请求 = 本轮已落盘的请求条数 + 在飞这一条，与 `--round` 的「请求数」同口径；
- 累计输入 = 本会话起点（`created_ts`）至今 `prompt_tokens` 之和，只用于 400k / 800k / 1.2M 判档；
  子代理请求同样写流水、无法从字段区分，一律计入（真实花费，不剔除）；
- 上下文 = 本轮首条「提问」的 `prompt_tokens` → 本轮末条已完成请求的 `prompt_tokens`；
  本轮还没有流水时显示「待落盘」，不拿上一任务的末条兜底。
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import _state

REPO = Path(__file__).resolve().parents[2]
AUDIT = Path(os.environ.get("DESKHUB_HOOK_AUDIT") or REPO / "data" / "hook_audit.log")
USAGE = Path(os.environ.get("DESKHUB_LLM_USAGE") or REPO / "data" / "llm_usage.jsonl")

BUDGET_LIMIT = 1_200_000
TIERS = (400_000, 800_000, 1_200_000)
CTX_WARNS = (40_000, 60_000, 80_000)
HANDOFF = ("- 已完成：", "- 未完成：", "- 下一步：", "- 约束：", "- 读取起点：")


def audit(obj: str, decision: str, detail: str = "") -> None:
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "hook": "PreToolUse(guard_budget)",
                        "object": obj[:120],
                        "decision": decision,
                        "detail": detail,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


def hint(text: str) -> None:
    print(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}},
            ensure_ascii=False,
        )
    )


def _k(tokens: int) -> str:
    return f"{tokens / 1000:.0f}k" if tokens >= 1000 else str(tokens)


def _epoch(text: object) -> float | None:
    """流水里的 ts 形如 `2026-10-28T13:16:37+0800`；解析不了就跳过这条。"""
    if not isinstance(text, str) or not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def load_rows(path: Path) -> list[tuple[float | None, int, str, str]]:
    """读流水 → [(ts, prompt_tokens, action, object)]；坏行跳过，读不到返回空表。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    rows: list[tuple[float | None, int, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else obj
        try:
            prompt = int(usage.get("prompt_tokens") or 0)
        except (TypeError, ValueError):
            continue
        rows.append(
            (_epoch(obj.get("ts")), prompt, str(obj.get("action") or ""), str(obj.get("object") or "-"))
        )
    return rows


def tier_text(
    tier: int, cumulative: int, rows: list[tuple[float | None, int, str]], requests: int
) -> list[str]:
    left = max(BUDGET_LIMIT - cumulative, 0)
    pct = round(cumulative / BUDGET_LIMIT * 100)
    head = f"预算：累计输入 {_k(cumulative)} / 1.2M（已用 {pct}%，剩 {_k(left)}）。"

    if tier < 800_000:
        return [head + "下一步合并同类动作、单轮工具输出 ≤200 行，不再开新探索。"]

    if tier < 1_200_000:
        by_object = Counter()
        for _, prompt, _action, obj in rows:
            by_object[obj[:40]] += prompt
        top = "、".join(f"`{name}` {_k(total)}" for name, total in by_object.most_common(3))
        repeats = [(name, count) for name, count in by_object.most_common() if count >= 2]
        if repeats:
            name, count = repeats[0]
            avg = cumulative // max(requests, 1)
            saving = f"`{name}` 出现 {count} 次，按均值 {_k(avg)}/条估算，合并可省约 {_k((count - 1) * avg)}"
        else:
            saving = "无明显重复对象"
        return [
            head,
            f"- Top3 对象：{top or '-'}",
            f"- 重复项：{saving}",
            "- 子代理请求无法从流水区分（无 session 字段），已计入总量",
            "- 建议：先出交接摘要（已完成 / 未完成 / 下一步 / 约束 / 读取起点），只读未完成部分",
        ]

    return [f"预算：累计输入 {_k(cumulative)} 已超 1.2M 上限，本任务收口，不再开新改动。", "交接摘要（照抄填写）：", *HANDOFF]


def ctx_warn_text(last_ctx: int) -> str:
    return (
        f"上下文已 {_k(last_ctx)}（40k 起每条请求都要重发全量历史）。"
        "剩余任务 >5 条建议换会话，先出交接摘要：\n" + "\n".join(HANDOFF)
    )


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

    now = int(time.time())
    with _state.locked():
        state = _state.load()
        slot = _state.bucket(state, session_id)

        created = int(slot.get("created_ts") or 0) or now
        slot["created_ts"] = created
        task_start = int(slot.get("task_start_ts") or 0) or created
        slot["task_start_ts"] = task_start

        data = load_rows(USAGE)
        session_rows = [row for row in data if row[0] is not None and row[0] >= created]
        cumulative = sum(row[1] for row in session_rows)
        task_rows = [row for row in session_rows if row[0] >= task_start]

        # 轮 / 请求都从流水推：本轮已落盘请求数 + 在飞这一条。一条请求里的并行工具调用
        # 同属一轮、也只算一次工具往返；不用状态文件计数——并行 hook 会互相覆盖（实测 3→1 回退）。
        rounds = requests = len(task_rows) + 1

        start_ctx = next(
            (row[1] for row in task_rows if row[2] == "提问"),
            task_rows[0][1] if task_rows else 0,
        )
        last_ctx = task_rows[-1][1] if task_rows else start_ctx
        ctx_text = f"{_k(start_ctx)}→{_k(last_ctx)}" if task_rows else "待落盘"

        lines = [
            f"预算快照：轮 {rounds} ｜ 请求 {requests} ｜ 上下文 {ctx_text}"
            "（照抄进回执「- 轮数：」行）"
        ]

        tier = max((step for step in TIERS if cumulative >= step), default=0)
        if tier and tier != int(slot.get("last_budget_tier") or 0):
            slot["last_budget_tier"] = tier
            lines += tier_text(tier, cumulative, session_rows, requests)

        ctx_level = max((step for step in CTX_WARNS if last_ctx >= step), default=0)
        if ctx_level > int(slot.get("last_ctx_warn") or 0):
            slot["last_ctx_warn"] = ctx_level
            lines.append(ctx_warn_text(last_ctx))

        _state.save(state)

    audit(str(payload.get("tool_name") or "-"), "hint", f"rounds={rounds} req={requests} cum={cumulative}")
    hint("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # fail-open：计量出错绝不阻断工具调用
        pass

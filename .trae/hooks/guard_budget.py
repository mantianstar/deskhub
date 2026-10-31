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
import math
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
TIER_REPEAT_AT = (1, 3, 6)  # 同一档只复述这几次：复述会进历史并被后续每条请求重发，再多就是噪音
CTX_WARNS = (60_000, 80_000, 100_000)
# 重启基线 = 段起点 p50：data/llm_usage.jsonl 2026-10-26~31 按 30 分钟切 32 段，段起点 p50 21.7k
# （本会话实测起点 21,779）。比值可算，但「3 倍」是量级估算不是精确模型：交接 ≈ 2 个基线轮
# （新会话首轮 22k + 一轮读文件），继续 2 轮 ≈ 2 × ctx；ctx ≥ 3 × 基线时续做比交接贵。
RESTART_BASELINE = 21_700
HANDOFF_PATH = "data/handoffs/{YYYY-MM-DD}-{slug}.md"
CLOSURE_LINE = "继续不算坚持，停止不算未完成"
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


def estimate_left_requests(left: int, last_ctx: int, prompts: list[int]) -> int | None:
    """还能发几条：按上下文线性增长解二次方程，与 token_cost.left_requests 同口径。

    d = 末 3 条 prompt_tokens 差分的均值（负值按 0）。每轮涨 d 时，n 轮累计增量
    ≈ n·ctx + d·n²/2 ≤ left，解得 n = (√(ctx² + 2·d·left) − ctx) / d。
    ctx 为 0（流水还没落盘）时不估，返回 None。
    """
    if last_ctx <= 0:
        return None
    deltas = [b - a for a, b in zip(prompts, prompts[1:])]
    d = sum(max(delta, 0) for delta in deltas) / len(deltas) if deltas else 0
    if d <= 0:
        return left // last_ctx
    return int((math.sqrt(last_ctx**2 + 2 * d * left) - last_ctx) / d)


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
    tier: int,
    cumulative: int,
    rows: list[tuple[float | None, int, str]],
    last_ctx: int,
    closure: bool = True,
) -> list[str]:
    left = max(BUDGET_LIMIT - cumulative, 0)
    pct = round(cumulative / BUDGET_LIMIT * 100)
    n = estimate_left_requests(left, last_ctx, [row[1] for row in rows[-3:]])
    head = f"预算：累计输入 {_k(cumulative)} / 1.2M（已用 {pct}%，剩 {_k(left)}）"
    if n is not None:
        head += f"；按当前上下文 {_k(last_ctx)} 还能发 {n} 条"
    head += "。"

    if tier < 800_000:
        return [
            head + "下一步合并同类动作、单轮工具输出 ≤200 行，不再开新探索"
            + (f"；{CLOSURE_LINE}。" if closure else "。")
        ]

    if tier < 1_200_000:
        by_object = Counter()
        counts = Counter()
        for _, prompt, _action, obj in rows:
            by_object[obj[:40]] += prompt
            counts[obj[:40]] += 1
        top = "、".join(f"`{name}` {_k(total)}" for name, total in by_object.most_common(3))
        repeats = [(name, count) for name, count in counts.most_common() if count >= 2]
        repeated = "、".join(f"`{name}` {count} 次" for name, count in repeats[:3]) or "无明显重复对象"
        return [
            head,
            f"- Top3 对象：{top or '-'}",
            f"- 重复项：{repeated}",
            "- 子代理请求无法从流水区分（无 session 字段），已计入总量",
            f"- 建议：现在把交接写到 {HANDOFF_PATH}（首行 status: pending，路径规则同上下文 80k 档），"
            "换会话续跑只读未完成部分" + (f"；{CLOSURE_LINE}。" if closure else "。"),
        ]

    return [
        f"预算：累计输入 {_k(cumulative)} 已超 1.2M 上限，本段会话收口。",
        "必须先落盘交接摘要，续跑放在新会话；新会话会先问你是否续跑，不会自动执行：",
        f"交接文件：{HANDOFF_PATH}（首行 status: pending），照抄下面五段：",
        *HANDOFF,
        *([f"{CLOSURE_LINE}。"] if closure else []),
    ]


def ctx_warn_text(last_ctx: int, closure: bool = True) -> str:
    """按档给不同内容：首行三档相同，附加内容递增（60k 一行 / 80k 写文件 / 100k 模板）。"""
    ratio = f"{last_ctx / RESTART_BASELINE:.1f}"
    head = (
        f"上下文 {_k(last_ctx)}，是重启基线（22k）的 {ratio} 倍；"
        "还需 2 轮以上的工作换会话更省，否则继续。"
    )
    if last_ctx < CTX_WARNS[1]:
        return head
    if last_ctx < CTX_WARNS[2]:
        return (
            head
            + f"\n若收不了口，把交接写到 {HANDOFF_PATH}，首行 status: pending，"
            "五段：已完成 / 未完成 / 下一步 / 约束 / 读取起点。"
        )
    return (
        head
        + f"\n必须先落盘：{HANDOFF_PATH}（首行 status: pending），照抄下面五段："
        + "".join(f"\n{line}" for line in HANDOFF)
        + (f"\n{CLOSURE_LINE}。" if closure else "")
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

        alert: list[str] = []  # 进 audit detail 的正文：预算提示 + 告警提示，不含已单独有的快照行

        closure_free = not slot.get("closure_line_sent")  # 停线全篇只发一次，哪档先触发算哪档

        tier = max((step for step in TIERS if cumulative >= step), default=0)
        if tier:
            if tier != int(slot.get("last_budget_tier") or 0):
                hits = 1
                slot["last_budget_tier"] = tier
            else:
                hits = int(slot.get("tier_hits") or 0) + 1
            slot["tier_hits"] = hits
            if hits in TIER_REPEAT_AT:
                body = tier_text(tier, cumulative, session_rows, last_ctx, closure_free)
                if closure_free and any(CLOSURE_LINE in line for line in body):
                    slot["closure_line_sent"] = True
                    closure_free = False
                lines += body
                alert += body

        ctx_level = max((step for step in CTX_WARNS if last_ctx >= step), default=0)
        if ctx_level > int(slot.get("last_ctx_warn") or 0):
            slot["last_ctx_warn"] = ctx_level
            warn = ctx_warn_text(last_ctx, closure_free)
            if closure_free and CLOSURE_LINE in warn:
                slot["closure_line_sent"] = True
            lines.append(warn)
            alert.append(warn)

        _state.save(state)

    detail = f"rounds={rounds} req={requests} cum={cumulative}"
    if alert:
        detail += "\n" + "\n".join(alert)
    audit(str(payload.get("tool_name") or "-"), "hint", detail)
    hint("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # fail-open：计量出错绝不阻断工具调用
        pass

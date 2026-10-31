"""PreToolUse(Read) 门禁：大文件必须分段读（RULES.md §四），重复部分必须换 offset。

四条判据：
1. 单段超过 120 行（project-rules.md「单段 ≤120 行」）→ 截到 120 行放行并提示；≥1,500 行才拒绝；
2. 请求区间已整段读过 → 提示后放行；
3. 重复率 > 80% 且新增 < 20 行 → 提示后放行，并把「该从第几行接着读」算给模型；
4. 同一文件第 3 次起，提示点名「再读一次 ≈ 72k」——提示本身也会被后续每条请求重发。

只提示不拒绝：一次 deny 约等于一次完整重发（约 5 万 token），截断 / 提示只有几百到几千 token。
部分重读（例如 limit 从 50 加到 75）放行，但把「重读了几行」回注进上下文。
审计固定记 offset / limit / new / repeat，事后能区分「同起点扩读」和「换位置正常读」。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import _state

MAX_LINES = 120
HARD_LINES = 1500  # 超过这个行数，放行代价大于一次完整重发，才值得拒绝
MAX_REPEAT_RATIO = 0.8
MIN_NEW_LINES = 20
NOISY_READ_COUNT = 3  # 同一文件读到第 3 次起，提示里点名单次成本
REREAD_COST = "72k"  # 一次重读 ≈ 72k token（全量上下文重发 + 新增内容）

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


def hint(text: str) -> None:
    """放行但补一句上下文：不拦这次，只纠正下一次的读法。"""
    print(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}},
            ensure_ascii=False,
        )
    )


def clamp(tool_input: dict, text: str) -> None:
    """把超限的 limit 截到上限后放行：不重发、不丢这次调用。"""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": tool_input,
                    "additionalContext": text,
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


def gaps(spans: list, start: int, end: int) -> list:
    """请求区间 [start, end) 里还没读过的片段；end 为文件行数，已读区间的 None 按 end 截断。"""
    out: list = []
    cursor = start
    for span_start, span_end in merge_spans(spans):
        stop = end if span_end is None else min(span_end, end)
        if stop <= cursor:
            continue
        if span_start > cursor:
            out.append((cursor, min(span_start, end)))
        cursor = max(cursor, stop)
        if cursor >= end:
            break
    if cursor < end:
        out.append((cursor, end))
    return [(left, right) for left, right in out if right > left]


def fingerprint(path: str):
    """文件指纹（mtime_ns + size）：内容变过就允许重读，避免编辑后无法回读验证。"""
    try:
        st = os.stat(path)
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return None


def cost_note(nth: int) -> str:
    """第 3 次起点名单次成本：同一句提示会随历史一起重发，说清代价比反复提醒有效。"""
    if nth < NOISY_READ_COUNT:
        return ""
    return f"该文件本轮已读 {nth} 次，再读一次 ≈ {REREAD_COST} token。"


def check(payload: dict, path: str, begin: int, end: int) -> tuple[str, str, str]:
    """返回 (decision, detail, text)：decision 取 allow / hint / deny，begin 为 0 基、end 为开区间。"""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return "allow", "", ""

    with _state.locked():
        state = _state.load()
        slot = _state.bucket(state, session_id)
        reads = slot.setdefault("reads", {})
        prints = slot.setdefault("prints", {})
        counts = slot.setdefault("read_counts", {})
        stamp = fingerprint(path)
        spans = reads.get(path) or []
        if stamp is not None and prints.get(path) != stamp:
            spans = []
            reads[path] = []
            prints[path] = stamp
            counts[path] = 0
        nth = int(counts.get(path) or 0) + 1
        counts[path] = nth

        holes = gaps(spans, begin, end)
        new = sum(stop - left for left, stop in holes)
        total = end - begin
        repeat = total - new
        detail = f"new={new} repeat={repeat} nth={nth}"

        if new <= 0:
            _state.save(state)
            return "hint", detail, (
                f"{path} 本轮第 {nth} 次读，第 {begin + 1}–{end} 行已在上下文；"
                f"往后从第 {end + 1} 行接着读，这些行不必再取。{cost_note(nth)}"
            )
        if repeat and repeat / total > MAX_REPEAT_RATIO and new < MIN_NEW_LINES:
            first = holes[0][0] + 1
            _state.save(state)
            return "hint", detail, (
                f"{path} 本轮第 {nth} 次读，第 {begin + 1}–{end} 行里 {repeat} 行已在上下文，"
                f"只有 {new} 行是新的；从第 {first} 行接着读（offset={first} limit={new}）。{cost_note(nth)}"
            )

        reads[path] = merge_spans(spans + [[begin, end]])
        _state.save(state)

    if not repeat:
        return "allow", detail, ""
    first = holes[0][0] + 1
    return "hint", detail, (
        f"{path} 本轮第 {nth} 次读，{repeat} 行已在上下文；新增从第 {first} 行起共 {new} 行，"
        f"下次从第 {first} 行接着读。{cost_note(nth)}"
    )


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

    raw_offset = tool_input.get("offset")
    try:
        offset = int(raw_offset) if raw_offset is not None else 1
    except (TypeError, ValueError):
        offset = 1
    begin = max(offset - 1, 0)

    if limit is not None and limit > MAX_LINES:
        if limit >= HARD_LINES:
            audit(path, "deny", f"limit={limit}>={HARD_LINES}")
            deny(
                f"Read 的 limit={limit} 一次带进 ≥{HARD_LINES} 行，放行代价超过一次完整重发（RULES.md §四）。"
                f"请按 ≤{MAX_LINES} 行分段读。"
            )
            return
        audit(path, "clamp", f"limit={limit}>{MAX_LINES}")
        clamp(
            {**tool_input, "offset": offset, "limit": MAX_LINES},
            f"Read 的 limit={limit} 超过单段 {MAX_LINES} 行，本次已截到 {MAX_LINES} 行放行；"
            f"接着读用 offset={offset + MAX_LINES}。",
        )
        return

    if limit is not None:
        decision, detail, text = check(payload, path, begin, begin + limit)
        audit(path, decision, f"limit={limit} offset={offset} {detail}".strip())
        if decision == "deny":
            deny(text)
        elif decision == "hint":
            hint(text)
        return

    total = line_count(path)
    if total is None:
        audit(path, "allow", f"offset={offset} binary skip")
        return

    remaining = total - begin
    if remaining <= 0:
        audit(path, "allow", f"offset={offset} beyond total={total}")
        return
    if remaining > MAX_LINES:
        if remaining >= HARD_LINES:
            audit(path, "deny", f"total={total} offset={offset}")
            deny(
                f"{path} 共 {total} 行，一次读 ≥{HARD_LINES} 行的代价超过一次完整重发（RULES.md §四）。"
                f"请带 offset/limit 分段读，单段 ≤{MAX_LINES} 行。"
            )
            return
        audit(path, "clamp", f"total={total} offset={offset}")
        clamp(
            {**tool_input, "offset": offset, "limit": MAX_LINES},
            f"{path} 共 {total} 行，本次只给第 {offset}–{offset + MAX_LINES - 1} 行；"
            f"接着读用 offset={offset + MAX_LINES}。",
        )
        return

    decision, detail, text = check(payload, path, begin, total)
    audit(path, decision, f"total={total} offset={offset} {detail}".strip())
    if decision == "deny":
        deny(text)
    elif decision == "hint":
        hint(text)


if __name__ == "__main__":
    main()

"""PreToolUse(Grep|Glob|LS) 门禁：搜过的词 / 目录不再搜第二遍。

三类判据：
1. 同一 (工具, 关键词, 范围, 过滤项) 第二次 → 追加提示、照常放行；
2. 上一次搜的范围包含这一次（先搜仓库根、再搜子目录）→ 追加提示、照常放行；
3. 其余放行，但每一次都落审计——搜索类动作以前完全没有记录，事后数不清本轮搜了几次。

只提示不拒绝：一次 deny 约等于一次完整重发（本仓库实测 5 万 token/条），
一次重复搜索只有几百 token。搜索没有输出体量问题（Grep 自带 head_limit），所以这里只判重、不判大小。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import _state

REPO = Path(__file__).resolve().parents[2]
AUDIT = Path(os.environ.get("DESKHUB_HOOK_AUDIT") or REPO / "data" / "hook_audit.log")
MAX_SEARCHES = 50
KNOWN = {"Grep", "Glob", "LS"}


def audit(tool: str, obj: str, decision: str, detail: str = "") -> None:
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "hook": f"PreToolUse({tool})",
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
    """放行但补一句上下文：不拦这次，只纠正下一次的搜法。"""
    print(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}},
            ensure_ascii=False,
        )
    )


def normalize(path: object) -> str:
    text = str(path or "").strip()
    if not text:
        return "."
    return os.path.normpath(text).rstrip("/") or "/"


def covers(parent: str, child: str) -> bool:
    if parent == child or parent == "/":
        return True
    if parent == ".":
        return not child.startswith("/")
    return child.startswith(parent.rstrip("/") + "/")


def describe(tool: str, tool_input: dict) -> dict:
    """把一次搜索压成可判重的条目；path 是范围，其余是「搜什么」。"""
    path = normalize(tool_input.get("path"))
    if tool == "Grep":
        pattern = " ".join(str(tool_input.get("pattern") or "").split())
        scope = "|".join(
            f"{key}={tool_input.get(key)}" for key in ("glob", "type", "output_mode", "head_limit") if tool_input.get(key)
        )
        if tool_input.get("-i"):
            scope += "|i"
        return {"tool": tool, "pattern": pattern, "scope": scope, "path": path}
    if tool == "Glob":
        return {"tool": tool, "pattern": str(tool_input.get("pattern") or ""), "scope": "", "path": path}
    return {"tool": tool, "pattern": "", "scope": "", "path": path}


def key_of(entry: dict) -> str:
    return f"{entry['tool']}|{entry['pattern']}|{entry['scope']}|{entry['path']}"


def label(entry: dict) -> str:
    return entry["pattern"] or entry["path"]


def explain(entry: dict, previous: dict) -> str:
    if previous["path"] != entry["path"] and covers(previous["path"], entry["path"]):
        return (
            f"{entry['tool']}「{label(entry)}」已经在更大范围 {previous['path']} 里搜过，"
            "缩小范围重搜拿不到新信息；直接看上一次的结果。"
        )
    return (
        f"{entry['tool']}「{label(entry)}」在 {entry['path']} 已搜过（RULES.md §二.4：同一事实只验证一次）。"
        "换关键词或换范围，不要原样重搜。"
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    tool = payload.get("tool_name") or payload.get("tool") or ""
    if tool not in KNOWN:
        return

    tool_input = payload.get("tool_input") or {}
    entry = describe(tool, tool_input)
    key = key_of(entry)

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        audit(tool, label(entry), "allow", f"path={entry['path']} no-session")
        return

    with _state.locked():
        state = _state.load()
        slot = _state.bucket(state, session_id)
        searches = slot.setdefault("searches", [])

        previous = next(
            (
                item
                for item in searches
                if isinstance(item, dict)
                and item.get("pattern") == entry["pattern"]
                and item.get("scope") == entry["scope"]
                and covers(str(item.get("path") or "."), entry["path"])
            ),
            None,
        )
        if previous is not None:
            audit(tool, label(entry), "hint", f"repeat path={entry['path']} prev={previous.get('path')}")
            hint("已放行，仅提示：" + explain(entry, previous))
            return

        searches.append(entry)
        slot["searches"] = searches[-MAX_SEARCHES:]
        _state.save(state)

    audit(tool, label(entry), "allow", f"record path={entry['path']}")


if __name__ == "__main__":
    main()

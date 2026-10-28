"""PreToolUse(RunCommand) 门禁：同一验证范围不重跑（RULES.md §二.4）。

只认「命令位」上的 pytest（`pytest ...`、`python -m pytest ...`），
`grep pytest` 这类查词不算测试，直接放行。

判重键不是原始命令串，而是「验证范围」：
- targets：测哪些路径 / 用例；
- behavior：改变结果口径的选项（`-k`、`-m`、`-x`、`--lf`、`-n` 等）。
管道、重定向、`cd X &&`、环境变量前缀、`-q / -v / --tb=*` 这类只影响观感的写法，
归一化后都不改变键——换成 `pytest tests -q 2>&1 | tail -20` 仍然算同一次。
换了测试范围直接跑；同一范围确需重跑，在命令末尾加 `# rerun`。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

import _state

REPO = Path(__file__).resolve().parents[2]
AUDIT = Path(os.environ.get("DESKHUB_HOOK_AUDIT") or REPO / "data" / "hook_audit.log")
MAX_CMDS = 50
RERUN_MARK = "# rerun"
TEST_RE = re.compile(r"(?:^|[;&|]\s*)(?:(?:\S*/)?python[\d.]*\s+-m\s+)?pytest\b")
SPLIT_RE = re.compile(r"&&|\|\||[;|]")
REDIRECT_RE = re.compile(r"\d?>>?\s*[^\s;&|]+|\d?>&\d+|<[^\s;&|]+")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
OUTPUT_FLAG = re.compile(
    r"^(?:-q|--quiet|-v|--verbose|--tb=.*|-r[a-zA-Z]*|--color(?:=.*)?|--no-header|"
    r"--no-summary|--durations(?:=.*)?|--show-capture(?:=.*)?|-s|--capture=.*)$"
)
# 带参数的选项：后面的值要跟着走，否则会被误当成测试路径
VALUE_FLAGS = {
    "-k",
    "-m",
    "-n",
    "-o",
    "-p",
    "-W",
    "--maxfail",
    "--ignore",
    "--ignore-glob",
    "--deselect",
    "--rootdir",
}


def audit(command: str, decision: str, detail: str = "") -> None:
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "hook": "PreToolUse(RunCommand)",
                        "object": command[:120],
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
    print(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}},
            ensure_ascii=False,
        )
    )


def canonical(command: str) -> tuple[str, str, str]:
    """拆出 (targets, behavior, canonical)：前两个进判重键，第三个进审计。"""
    segment = next((part for part in SPLIT_RE.split(command) if TEST_RE.search(part)), command)
    segment = REDIRECT_RE.sub(" ", segment).strip()
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()

    index = next((i for i, token in enumerate(tokens) if token.endswith("pytest")), None)
    if index is not None:
        tokens = tokens[index + 1 :]

    targets: list[str] = []
    behavior: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            behavior[-1] = f"{behavior[-1]}={token}"
            skip_next = False
            continue
        if ENV_ASSIGN.match(token) or OUTPUT_FLAG.match(token):
            continue
        if token.startswith("-"):
            behavior.append(token)
            skip_next = token in VALUE_FLAGS
        else:
            targets.append(token.rstrip("/") or "/")

    target_key = " ".join(sorted(targets))
    behavior_key = " ".join(behavior)
    return target_key, behavior_key, f"{target_key} | {behavior_key}".strip(" |")


def _entry_key(entry: object) -> str:
    """兼容旧状态里直接存 sha1 字符串的写法。"""
    if isinstance(entry, dict):
        return str(entry.get("key") or "")
    return entry if isinstance(entry, str) else ""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not TEST_RE.search(command):
        return

    norm = " ".join(command.split())
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    if RERUN_MARK in norm:
        _, _, canon = canonical(norm)
        audit(norm, "hint", "rerun")
        hint(
            f"重跑已放行（范围：{canon or '同上次'}）。只是个别用例失败时只重跑失败文件："
            "`pytest <失败文件> -q`，不要全量重跑——全量一次比只跑失败文件多一倍输入。"
        )
        return

    targets, behavior, canon = canonical(norm)
    key = hashlib.sha1(canon.encode("utf-8")).hexdigest()

    with _state.locked():
        state = _state.load()
        slot = _state.bucket(state, session_id)
        cmds = slot.setdefault("cmds", [])

        if any(_entry_key(entry) == key for entry in cmds):
            audit(norm, "deny", f"repeat canon={canon}")
            deny(
                "这条 pytest 的验证范围已在本会话执行过（RULES.md §二.4：同一事实只验证一次）。"
                f"本次判重口径：{canon or '（同一命令）'}；管道、重定向、输出格式参数的变化不算新的一次。"
                "换了测试范围直接跑；同一范围确需重跑，请在命令末尾追加 `# rerun`。"
            )
            return

        same_target = next(
            (
                entry
                for entry in cmds
                if isinstance(entry, dict) and entry.get("targets") == targets and entry.get("behavior") != behavior
            ),
            None,
        )
        cmds.append({"key": key, "targets": targets, "behavior": behavior, "raw": norm[:120]})
        slot["cmds"] = cmds[-MAX_CMDS:]
        _state.save(state)

    if same_target is not None:
        audit(norm, "hint", f"record canon={canon}")
        hint(
            f"同一批测试目标这次换了口径（{same_target.get('behavior') or '无'} → {behavior or '无'}），"
            "不算重跑，已放行；结论与上次不可直接比较。"
        )
        return
    audit(norm, "allow", f"record canon={canon}")


if __name__ == "__main__":
    main()

"""门禁契约：hook 里的硬编码必须与常驻规则、RULES.md、hooks.json 注册表一致。

数值只定义在 `.trae/rules/project-rules.md`（它每轮进上下文，模型才可能事前遵守）；
hook 消费同一数字，本文件锁住三方不许漂移。运行时 hook 不解析 markdown ——
解析失败会误拦，违背「门禁绝不因自身问题阻断」的前提。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".trae" / "hooks"
RULES = ROOT / "RULES.md"
SNIPPET = ROOT / ".trae" / "rules" / "project-rules.md"
CONFIG = ROOT / ".trae" / "hooks.json"


def _source(script: str) -> str:
    return (HOOKS / script).read_text(encoding="utf-8")


def _const(script: str, name: str) -> str:
    match = re.search(rf"^{name} = ([\d.]+)$", _source(script), re.M)
    assert match, f"{script} 里找不到常量 {name}"
    return match.group(1)


def test_read_thresholds_match_snippet():
    """guard_read 的 120 / 0.8 / 20 必须与常驻摘要里的数字逐字一致。"""
    snippet = SNIPPET.read_text(encoding="utf-8")
    limit = _const("guard_read.py", "MAX_LINES")
    ratio = _const("guard_read.py", "MAX_REPEAT_RATIO")
    fresh = _const("guard_read.py", "MIN_NEW_LINES")

    assert f"单段 ≤{limit} 行" in snippet, "MAX_LINES 与摘要的「单段 ≤N 行」不一致"
    assert f"重复率 > {ratio}" in snippet, "MAX_REPEAT_RATIO 与摘要不一致"
    assert f"新增 < {fresh} 行" in snippet, "MIN_NEW_LINES 与摘要不一致"


def test_rules_separates_segment_and_round_caps():
    """200 行是「整轮累计」，120 行是「单次 Read 段」——两个指标必须写明，不能只看数字。"""
    text = RULES.read_text(encoding="utf-8")
    assert "单轮工具输出累计 ≤200 行 / 8KB" in text
    assert "单次 `Read` 段 ≤120 行" in text
    assert "project-rules.md" in text


def test_stop_hook_checks_structure_not_keywords():
    """回执不能只靠关键词蒙过：首行独立「回执：」、轮数行、成本行都要在。"""
    hook = _source("guard_stop.py")
    assert "ROUND_LINE_RE" in hook and re.search(r'ROUND_LINE_RE = re\.compile\(', hook)
    assert "COST_LINE_RE" in hook
    assert "合计" in hook and "未计量" in hook
    rules = RULES.read_text(encoding="utf-8")
    assert "回执" in rules and "成本清单" in rules and "- 轮数：" in rules
    snippet = SNIPPET.read_text(encoding="utf-8")
    assert "- 轮数：" in snippet
    assert "- 成本：输入" in snippet and "- 成本：未计量" in snippet


def test_every_guard_script_is_registered():
    """脚本存在但 hooks.json 漏注册 = 静默失效，必须在测试里暴露。"""
    registered = set(re.findall(r"guard_\w+\.py", CONFIG.read_text(encoding="utf-8")))
    present = {path.name for path in HOOKS.glob("guard_*.py")}
    assert present - registered == set(), f"hooks.json 漏注册：{present - registered}"
    assert registered - present == set(), f"hooks.json 引用了不存在的脚本：{registered - present}"


def test_search_hook_matches_all_three_tools():
    """Grep / Glob / LS 三件套同一个 matcher，漏一个就等于该类动作无门禁。"""
    assert "^(Grep|Glob|LS)$" in CONFIG.read_text(encoding="utf-8")


def test_hooks_json_is_valid_json():
    json.loads(CONFIG.read_text(encoding="utf-8"))


def test_q_cap_and_round_definition_locked():
    """Q 文件上限、轮数定义、判重口径必须写进对应文档，防止再次漂移。"""
    routing = (ROOT / "docs" / "task-routing.md").read_text(encoding="utf-8")
    assert "≤8 个文件" in routing
    assert "≤3 个文件、≤3 轮" not in routing
    rules = RULES.read_text(encoding="utf-8")
    assert "一轮 = 一条助手回复" in rules
    assert "同区间重读（重复率 > 80% 且新增 < 20 行）" in rules
    assert "同一文件多次读" not in rules


def test_snippet_whole_file_read_cap_matches_hook():
    """整段读阈值必须 ≤ hook 的 MAX_LINES，否则按规则做会被 hook 拒。"""
    assert "文件 <120 行直接整段读" in SNIPPET.read_text(encoding="utf-8")


def test_write_audit_and_reset_hooks_registered():
    """写审计与任务边界重置必须有注册，漏挂等于没做。"""
    config = CONFIG.read_text(encoding="utf-8")
    assert "UserPromptSubmit" in config
    assert "^(Write|Edit|SearchReplace|DeleteFile)$" in config


def test_read_state_resets_on_user_prompt_and_content_change(tmp_path):
    """同区间二次读被拒；用户新消息或文件改动后重新放行。"""
    import os
    import subprocess
    import sys

    target = tmp_path / "sample.txt"
    target.write_text("a\n" * 40, encoding="utf-8")
    env = {
        **os.environ,
        "DESKHUB_HOOK_STATE": str(tmp_path / "state.json"),
        "DESKHUB_HOOK_AUDIT": str(tmp_path / "audit.log"),
    }

    def run(script: str, payload: dict) -> str:
        return subprocess.run(
            [sys.executable, str(HOOKS / script)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
        ).stdout

    read = {"session_id": "t", "tool_input": {"file_path": str(target), "offset": 1, "limit": 10}}
    assert '"permissionDecision": "deny"' not in run("guard_read.py", read)
    assert '"permissionDecision": "deny"' in run("guard_read.py", read)

    run("guard_reset.py", {"session_id": "t"})
    assert '"permissionDecision": "deny"' not in run("guard_read.py", read)

    assert '"permissionDecision": "deny"' in run("guard_read.py", read)
    target.write_text("b\n" * 41, encoding="utf-8")
    assert '"permissionDecision": "deny"' not in run("guard_read.py", read)


# --- 成本口径与预算提示：guard_budget 只注入不拦，guard_stop 认不带「/ 上限」的轮数行 ---


def _stamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _budget_env(tmp_path: Path, prompts: list[int], seed: dict | None = None) -> tuple[dict, Path]:
    """造一个 10 分钟前开始的会话槽 + 对应的 usage 夹具，返回子进程 env。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    created = int(time.time()) - 600
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "sessions": {
                    "s1": {
                        "ts": created,
                        "created_ts": created,
                        "task_start_ts": created,
                        "reads": {},
                        "searches": [],
                        "cmds": [],
                        **(seed or {}),
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    usage = tmp_path / "usage.jsonl"
    usage.write_text(
        "".join(
            json.dumps({"ts": _stamp(created + 60 + index), "object": f"f{index}.md", "prompt_tokens": prompt})
            + "\n"
            for index, prompt in enumerate(prompts)
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "DESKHUB_HOOK_STATE": str(state_path),
        "DESKHUB_HOOK_AUDIT": str(tmp_path / "audit.log"),
        "DESKHUB_LLM_USAGE": str(usage),
    }
    return env, state_path


def _run_budget(env: dict) -> str:
    return subprocess.run(
        [sys.executable, str(HOOKS / "guard_budget.py")],
        input=json.dumps({"session_id": "s1", "tool_name": "Read"}),
        capture_output=True,
        text=True,
        env=env,
    ).stdout


def _context(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_budget_hook_rounds_follow_logged_requests(tmp_path: Path):
    """轮 / 请求都按本轮已落盘请求数 +1 算，不受工具调用次数或状态文件影响。"""
    env, _ = _budget_env(tmp_path, [20_000])
    first = _context(_run_budget(env))
    second = _context(_run_budget(env))
    assert "轮 2 ｜ 请求 2" in first and "轮 2 ｜ 请求 2" in second
    assert '"permissionDecision"' not in _run_budget(env)


def test_budget_hook_context_starts_at_round_first_question(tmp_path: Path):
    """起点认本轮首条「提问」，不拿上一任务末条兜底；结束取本轮末条。"""
    env, _ = _budget_env(tmp_path, [20_000], {"task_start_ts": int(time.time()) - 100})
    now = int(time.time())
    rows = [
        {"ts": _stamp(now - 300), "action": "命令", "object": "上个任务", "prompt_tokens": 68_366},
        {"ts": _stamp(now - 60), "action": "提问", "object": "提交并推送 git", "prompt_tokens": 20_492},
        {"ts": _stamp(now - 58), "action": "命令", "object": "git diff", "prompt_tokens": 21_961},
    ]
    (tmp_path / "usage.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    out = _context(_run_budget(env))
    assert "上下文 20k→22k" in out
    assert "68k" not in out


def test_budget_hook_escalates_three_tiers(tmp_path: Path):
    """400k / 800k / 1.2M 三档文案逐级升级，且都只是注入文字。"""
    cases = {
        "t400": ([200_000, 210_000], "剩"),
        "t800": ([400_000, 420_000], "Top3"),
        "t1200": ([600_000, 620_000], "交接摘要"),
    }
    for name, (prompts, needle) in cases.items():
        env, _ = _budget_env(tmp_path / name, prompts)
        out = _run_budget(env)
        assert needle in _context(out), name
        assert '"permissionDecision"' not in out, name


def test_budget_hook_fails_open_without_usage(tmp_path: Path):
    """读不到 usage 流水只出快照，不拦、不崩。"""
    env, _ = _budget_env(tmp_path, [])
    env["DESKHUB_LLM_USAGE"] = str(tmp_path / "missing.jsonl")
    out = _run_budget(env)
    assert '"permissionDecision"' not in out
    assert "预算快照" in _context(out)


def test_budget_hook_warns_context_once(tmp_path: Path):
    """40k 封顶提示只在跨档时注入一次，之后只出快照一行。"""
    env, _ = _budget_env(tmp_path, [47_000])
    first = _context(_run_budget(env))
    second = _context(_run_budget(env))
    assert "40k 起" in first and "交接摘要" in first
    assert "40k 起" not in second and "预算快照" in second


def test_pytest_hook_hints_rerun_only_failed_files(tmp_path: Path):
    """`# rerun` 放行，但注入「只重跑失败文件」。"""
    env = {
        **os.environ,
        "DESKHUB_HOOK_STATE": str(tmp_path / "state.json"),
        "DESKHUB_HOOK_AUDIT": str(tmp_path / "audit.log"),
    }

    def run(command: str) -> str:
        return subprocess.run(
            [sys.executable, str(HOOKS / "guard_pytest.py")],
            input=json.dumps({"session_id": "s1", "tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            env=env,
        ).stdout

    assert run("pytest tests -q").strip() == ""
    head = json.loads(run("pytest tests -q # rerun"))["hookSpecificOutput"]
    assert "permissionDecision" not in head
    assert "只重跑失败文件" in head["additionalContext"]


def test_stop_hook_accepts_batch_round_line(tmp_path: Path):
    """新轮数行（不带「/ 上限」）放行；缺成本行仍然 block。"""
    env = {**os.environ, "DESKHUB_HOOK_AUDIT": str(tmp_path / "audit.log")}
    message = (
        "回执：\n- 轮数：3 ｜ 请求：4 ｜ 上下文：20,447→44,057\n"
        "- 搜索/读取：Grep×3 ｜ 输出峰：6KB ｜ 越界：无\n"
        "- 成本：输入 342,213 + 输出 8,586 = 合计 350,799"
    )

    def run(text: str) -> str:
        return subprocess.run(
            [sys.executable, str(HOOKS / "guard_stop.py")],
            input=json.dumps({"last_assistant_message": text}),
            capture_output=True,
            text=True,
            env=env,
        ).stdout

    assert run(message).strip() == ""
    assert '"decision": "block"' in run(message.rsplit("\n", 1)[0])

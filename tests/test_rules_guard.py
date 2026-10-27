"""规则门禁：把 RULES.md 里能机械判定的约束变成 pytest 用例。

`pytest tests -q` 即生效。三条：全文 ≤100 行、`§x.y` 引用真实存在、提交必带 CHANGELOG 条目。
CHANGELOG 那条只看暂存区，没 `git add` 过东西时跳过。
后四条跑 `.trae/hooks/` 的两个门禁脚本（假 stdin），防「脚本改了却没生效」。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "RULES.md"

_H2 = re.compile(r"^##\s+([零一二三四五六七八九十]+)、")
_ITEM = re.compile(r"^(\d+)\.\s")
_REF = re.compile(r"§([零一二三四五六七八九十]+)(?:\.(\d+))?")


def _sections(text: str) -> dict[str, set[int]]:
    """章节号 → 该节下 `N.` 开头的条目号，如 {"零": {1, 2, 3, 4, 5}, "二": {1..5}}。"""
    sections: dict[str, set[int]] = {}
    current = ""
    for line in text.splitlines():
        heading = _H2.match(line)
        if heading:
            current = heading.group(1)
            sections[current] = set()
            continue
        item = _ITEM.match(line)
        if current and item:
            sections[current].add(int(item.group(1)))
    return sections


def test_rules_under_100_lines():
    """维护约束：全文 ≤100 行，超了就把细则挪去 docs/ 分册。"""
    lines = RULES.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 100, f"RULES.md 已 {len(lines)} 行，上限 100 行：细则挪到 docs/ 分册，本文只留链接"


def test_rules_section_refs_exist():
    """RULES.md 内部的 §零.3 之类引用，必须指向真实存在的章节 / 条目。"""
    text = RULES.read_text(encoding="utf-8")
    sections = _sections(text)
    missing: list[str] = []
    for section, item in sorted({(m.group(1), m.group(2)) for m in _REF.finditer(text)}):
        if section not in sections:
            missing.append(f"§{section}（无此章节）")
        elif item is not None and int(item) not in sections[section]:
            missing.append(f"§{section}.{item}（{section} 节没有第 {item} 条）")
    assert not missing, "RULES.md 存在失效引用：" + "、".join(missing)


def test_changelog_entry_when_staged():
    """每次提交都要一条 CHANGELOG；有暂存改动却没带 CHANGELOG.md 就不许提交。"""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("拿不到暂存区（非 git 仓库），跳过")
    staged = [name for name in result.stdout.splitlines() if name.strip()]
    if not staged:
        pytest.skip("暂存区为空，无需校验")
    assert "CHANGELOG.md" in staged, (
        f"暂存了 {len(staged)} 个文件但没有 CHANGELOG.md：按 RULES.md §六 补一条条目再提交"
    )


# --- Hook 门禁：v1.82 加了脚本但运行时没加载，靠这两组用例保证「脚本坏了」能被发现 ---

HOOKS = ROOT / ".trae" / "hooks"


def _run_hook(
    script: str, payload: dict, audit_path: Path, state_path: Path | None = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, "DESKHUB_HOOK_AUDIT": str(audit_path)}
    if state_path is not None:
        env["DESKHUB_HOOK_STATE"] = str(state_path)
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )


def _audit_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture()
def big_file(tmp_path: Path) -> Path:
    path = tmp_path / "big.md"
    path.write_text("\n".join(f"line {i}" for i in range(300)), encoding="utf-8")
    return path


def test_read_hook_denies_whole_large_file(big_file: Path, tmp_path: Path):
    """>200 行且不带 limit 的整份读必须拒绝，并落一条 deny 审计。"""
    audit_path = tmp_path / "audit.log"
    result = _run_hook("guard_read.py", {"tool_input": {"file_path": str(big_file)}}, audit_path)
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert [row["decision"] for row in _audit_rows(audit_path)] == ["deny"]


def test_read_hook_allows_segmented_read(big_file: Path, tmp_path: Path):
    """带 limit ≤200 的分段读必须放行，并落一条 allow 审计。"""
    audit_path = tmp_path / "audit.log"
    result = _run_hook(
        "guard_read.py", {"tool_input": {"file_path": str(big_file), "limit": 120}}, audit_path
    )
    assert result.stdout.strip() == ""
    assert [row["decision"] for row in _audit_rows(audit_path)] == ["allow"]


def _read_hook(big_file: Path, tmp_path: Path, **tool_input) -> subprocess.CompletedProcess:
    payload = {"session_id": "s1", "tool_input": {"file_path": str(big_file), **tool_input}}
    return _run_hook("guard_read.py", payload, tmp_path / "audit.log", tmp_path / "state.json")


def test_read_hook_audits_offset_and_new_lines(big_file: Path, tmp_path: Path):
    """放行时审计要带 offset 与真实新增行数，才能事后核对「必要读」还是「重读」。"""
    result = _read_hook(big_file, tmp_path, offset=11, limit=50)
    assert result.stdout.strip() == ""
    row = _audit_rows(tmp_path / "audit.log")[0]
    assert row["decision"] == "allow"
    assert "offset=11" in row["detail"]
    assert "new=50" in row["detail"] and "repeat=0" in row["detail"]


def test_read_hook_hints_partial_repeat(big_file: Path, tmp_path: Path):
    """扩 limit 但有新增行：放行并提示下次只取没读过的部分。"""
    _read_hook(big_file, tmp_path, limit=50)
    payload = json.loads(_read_hook(big_file, tmp_path, limit=75).stdout)
    assert "permissionDecision" not in payload["hookSpecificOutput"]
    assert "重读" in payload["hookSpecificOutput"]["additionalContext"]


def test_read_hook_denies_shifted_window(big_file: Path, tmp_path: Path):
    """整段平移只多几行时拒绝，并给出可直接照抄的 offset。"""
    _read_hook(big_file, tmp_path, limit=120)
    payload = json.loads(_read_hook(big_file, tmp_path, offset=11, limit=120).stdout)
    head = payload["hookSpecificOutput"]
    assert head["permissionDecision"] == "deny"
    assert "offset=121" in head["permissionDecisionReason"]


def test_read_hook_denies_limit_over_cap(big_file: Path, tmp_path: Path):
    """单段上限和常驻规则里的数字必须一致，超了直接拒绝。"""
    payload = json.loads(_read_hook(big_file, tmp_path, limit=121).stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_read_limit_matches_rules_snippet():
    """hook 的 MAX_LINES 与 project-rules.md 的「单段 ≤N 行」必须是同一个数。"""
    import re

    hook = (HOOKS / "guard_read.py").read_text(encoding="utf-8")
    limit = int(re.search(r"^MAX_LINES = (\d+)$", hook, re.M).group(1))
    rules = (ROOT / ".trae" / "rules" / "project-rules.md").read_text(encoding="utf-8")
    assert f"单段 ≤{limit} 行" in rules


def test_read_hook_serializes_concurrent_duplicates(big_file: Path, tmp_path: Path):
    """并行读同一段只登记一次：flock 把 load→改→save 串起来，否则会丢登记。"""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: _read_hook(big_file, tmp_path, limit=50), range(6)))

    decisions = [row["decision"] for row in _audit_rows(tmp_path / "audit.log")]
    assert decisions.count("allow") == 1
    assert decisions.count("deny") == 5


def _pytest_hook(command: str, tmp_path: Path) -> subprocess.CompletedProcess:
    payload = {"session_id": "s1", "tool_input": {"command": command}}
    return _run_hook("guard_pytest.py", payload, tmp_path / "audit.log", tmp_path / "state.json")


def test_pytest_hook_denies_same_target_with_pipe(tmp_path: Path):
    """管道、重定向、tail 只改输出形态，不构成新的一次验证。"""
    assert _pytest_hook("pytest tests -q", tmp_path).stdout.strip() == ""
    payload = json.loads(_pytest_hook("pytest tests -q 2>&1 | tail -20", tmp_path).stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pytest_hook_allows_different_target(tmp_path: Path):
    """换测试目标是新事实，放行。"""
    _pytest_hook("pytest tests -q", tmp_path)
    assert _pytest_hook("pytest tests/test_x.py -q", tmp_path).stdout.strip() == ""


def test_pytest_hook_hints_when_behavior_changes(tmp_path: Path):
    """目标相同但口径变了（-x）：放行并提醒两次结论不可直接比较。"""
    _pytest_hook("pytest tests -q", tmp_path)
    payload = json.loads(_pytest_hook("pytest tests -x -q", tmp_path).stdout)
    assert "permissionDecision" not in payload["hookSpecificOutput"]


def _search_hook(tool: str, tool_input: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    payload = {"session_id": "s1", "tool_name": tool, "tool_input": tool_input}
    return _run_hook("guard_search.py", payload, tmp_path / "audit.log", tmp_path / "state.json")


def test_search_hook_denies_repeated_grep(tmp_path: Path):
    """同一 pattern 同一范围搜两次，第二次拒绝，且两次都落审计。"""
    assert _search_hook("Grep", {"pattern": "foo", "path": "tests"}, tmp_path).stdout.strip() == ""
    payload = json.loads(
        _search_hook("Grep", {"pattern": "foo", "path": "tests"}, tmp_path).stdout
    )
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert [row["decision"] for row in _audit_rows(tmp_path / "audit.log")] == ["allow", "deny"]


def test_search_hook_denies_narrower_path(tmp_path: Path):
    """父目录搜过再缩到子目录，结果已被覆盖，拿不到新信息。"""
    _search_hook("Grep", {"pattern": "foo", "path": "."}, tmp_path)
    payload = json.loads(_search_hook("Grep", {"pattern": "foo", "path": "tests"}, tmp_path).stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_search_hook_denies_repeated_ls(tmp_path: Path):
    """同目录 LS 两次也是重复事实。"""
    _search_hook("LS", {"path": "tests"}, tmp_path)
    payload = json.loads(_search_hook("LS", {"path": "tests"}, tmp_path).stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_stop_hook_blocks_missing_receipt(tmp_path: Path):
    """回复缺「回执：」和成本清单时必须 block。"""
    audit_path = tmp_path / "audit.log"
    result = _run_hook("guard_stop.py", {"last_assistant_message": "做完了"}, audit_path)
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "回执：" in payload["reason"]
    assert [row["decision"] for row in _audit_rows(audit_path)] == ["block"]


def test_stop_hook_allows_complete_receipt(tmp_path: Path):
    """回执三段结构与成本数字都在时放行，不输出 block。"""
    audit_path = tmp_path / "audit.log"
    message = (
        "回执：\n- 轮数：2 / 10\n- 搜索 1 次 / 读取 1 次\n- 越界项：无\n\n"
        "- 成本：输入 1,200 + 输出 34 = 合计 1,234"
    )
    result = _run_hook("guard_stop.py", {"last_assistant_message": message}, audit_path)
    assert result.stdout.strip() == ""
    assert [row["decision"] for row in _audit_rows(audit_path)] == ["allow"]


def test_stop_hook_blocks_unstructured_round_count(tmp_path: Path):
    """只写「回执：」不够：轮数要写成 N / M，写「2 轮」不算。"""
    audit_path = tmp_path / "audit.log"
    message = "回执：\n- 轮数 2 轮\n- 搜索 1 次\n\ntoken 成本 1234"
    result = _run_hook("guard_stop.py", {"last_assistant_message": message}, audit_path)
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "轮数" in payload["reason"]


def test_stop_hook_blocks_cost_without_number(tmp_path: Path):
    """成本清单必须给数字或明确「无新增」，只提「token 成本」两词不算。"""
    audit_path = tmp_path / "audit.log"
    message = "回执：\n- 轮数：2 / 10\n\ntoken 成本见上一轮"
    result = _run_hook("guard_stop.py", {"last_assistant_message": message}, audit_path)
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "成本" in payload["reason"]


def test_stop_hook_allows_no_new_requests(tmp_path: Path):
    """没有新请求时写「无新增」即可，不强制编一个数字。"""
    audit_path = tmp_path / "audit.log"
    message = (
        "回执：\n- 轮数：2 / 10\n- 搜索与读取：各 1 次\n- 越界项：无\n\n"
        "- 成本：未计量（以平台账单为准）"
    )
    result = _run_hook("guard_stop.py", {"last_assistant_message": message}, audit_path)
    assert result.stdout.strip() == ""


def test_stop_hook_allows_pasted_cost_table(tmp_path: Path):
    """贴明细表可以，但固定成本行必须原样保留（表格本身不参与判定）。"""
    audit_path = tmp_path / "audit.log"
    message = (
        "回执：\n- 轮数：2 / 10\n- 搜索与读取：各 1 次\n- 越界项：无\n\n"
        "- 成本：输入 117,398 + 输出 2,445 = 合计 119,843\n"
        "| 请求 | 动作 | 对象 | 输入 | 输出 | 成本 |\n|---|---|---|---|---|---|\n"
        "| **合计** | - | - | **117,398** | **2,445** | **119,843** |"
    )
    result = _run_hook("guard_stop.py", {"last_assistant_message": message}, audit_path)
    assert result.stdout.strip() == ""


def test_stop_hook_allows_numbered_receipt_with_unmetered_cost(tmp_path: Path):
    """编了号的回执也放行：只要轮数行与成本行是固定格式。"""
    audit_path = tmp_path / "audit.log"
    message = (
        "回执：\n1. 轮数：2 / 15\n2. 搜索与读取：搜索 1 次；读取 0 个文件\n"
        "3. 写入与命令：写入 无；执行命令 3 次\n\n"
        "- 成本：未计量（仅 3 条短命令，未跑 `--round`，游标不动）。"
    )
    result = _run_hook("guard_stop.py", {"last_assistant_message": message}, audit_path)
    assert result.stdout.strip() == ""


def test_stop_hook_blocks_non_fixed_round_line(tmp_path: Path):
    """轮数行也只认固定格式：「轮数：2 轮（/15）」「实际轮数 / 上限：2 / 10」都不算。"""
    audit_path = tmp_path / "audit.log"
    for rounds in ("1. 轮数：2 轮（/15）", "- 实际轮数 / 上限：2 / 10", "- 轮数 2 / 10"):
        message = f"回执：\n{rounds}\n\n- 成本：未计量（以平台账单为准）"
        result = _run_hook("guard_stop.py", {"last_assistant_message": message}, audit_path)
        payload = json.loads(result.stdout)
        assert payload["decision"] == "block", rounds
        assert "轮数" in payload["reason"], rounds


def test_stop_hook_allows_cost_followed_by_token_cost_mention(tmp_path: Path):
    """成本行之后又提到 `token_cost.py` 时仍应放行（按最后一个 token 切段会误判）。"""
    audit_path = tmp_path / "audit.log"
    message = (
        "回执：\n- 轮数：2 / 10\n- 搜索与读取：各 1 次\n- 越界项：无\n\n"
        "- 成本：输入 117,398 + 输出 2,445 = 合计 119,843"
        "（明细已由 `token_cost.py --round` 出表）"
    )
    result = _run_hook("guard_stop.py", {"last_assistant_message": message}, audit_path)
    assert result.stdout.strip() == ""


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


def _run_hook(script: str, payload: dict, audit_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "DESKHUB_HOOK_AUDIT": str(audit_path)},
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


def test_stop_hook_blocks_missing_receipt(tmp_path: Path):
    """回复缺「回执：」和成本清单时必须 block。"""
    audit_path = tmp_path / "audit.log"
    result = _run_hook("guard_stop.py", {"last_assistant_message": "做完了"}, audit_path)
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "回执：" in payload["reason"]
    assert [row["decision"] for row in _audit_rows(audit_path)] == ["block"]


def test_stop_hook_allows_complete_receipt(tmp_path: Path):
    """回执和成本清单都在时放行，不输出 block。"""
    audit_path = tmp_path / "audit.log"
    message = "回执：1. 轮数 2 轮\n2. 搜索 1 次\n3. 本轮 token 成本 1234"
    result = _run_hook("guard_stop.py", {"last_assistant_message": message}, audit_path)
    assert result.stdout.strip() == ""
    assert [row["decision"] for row in _audit_rows(audit_path)] == ["allow"]

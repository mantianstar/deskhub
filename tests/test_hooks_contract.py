"""门禁契约：hook 里的硬编码必须与常驻规则、RULES.md、hooks.json 注册表一致。

数值只定义在 `.trae/rules/project-rules.md`（它每轮进上下文，模型才可能事前遵守）；
hook 消费同一数字，本文件锁住三方不许漂移。运行时 hook 不解析 markdown ——
解析失败会误拦，违背「门禁绝不因自身问题阻断」的前提。
"""

from __future__ import annotations

import json
import re
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
    """回执不能只靠关键词蒙过：轮数要 N / M，成本清单只认固定行。"""
    hook = _source("guard_stop.py")
    assert "ROUNDS_RE" in hook and re.search(r'ROUNDS_RE = re\.compile\(r"', hook)
    assert "COST_LINE_RE" in hook
    assert "合计" in hook and "未计量" in hook
    rules = RULES.read_text(encoding="utf-8")
    assert "回执" in rules and "成本清单" in rules
    snippet = SNIPPET.read_text(encoding="utf-8")
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

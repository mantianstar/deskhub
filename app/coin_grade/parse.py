"""解析第二阶段品相评级输出（固定七行格式，见 app/coin_grade/prompt.py）。

模型偶尔会漏行或换全角符号：这里只做宽松提取，缺项返回 None，不猜测。
"""

from __future__ import annotations

import re

# 评级前缀，长的在前，避免 VF 被 F 抢先匹配
_GRADE_RE = re.compile(r"(VG|XF|VF|AU|MS|G|F)\s*(\d{1,2})")
_BEST_PREFIXES = {"XF", "AU", "MS"}

_LABELS = {
    "磨损": "wear_desc",
    "铜色": "color_desc",
    "瑕疵": "flaws",
    "瑕疵与盲区": "blind_spots",
    "盲区": "blind_spots",
    "置信度": "confidence",
}


def _grade_prefix(interval: str | None) -> str | None:
    if not interval:
        return None
    match = _GRADE_RE.search(interval)
    return match.group(1) if match else None


def parse_grade(text: str | None) -> dict[str, object]:
    """六行文本 → 结构化字段；评级行按「区间 / 中文品相 / 铜色等级 / 净分」拆。"""
    parsed: dict[str, object] = {
        "grade_interval": None,
        "grade_score": None,
        "grade_cn": None,
        "color_grade": None,
        "wear_desc": None,
        "color_desc": None,
        "flaws": None,
        "blind_spots": None,
        "confidence": None,
        "net_grade": None,
    }
    if not text:
        return parsed

    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("-*#").strip()
        if not line:
            continue
        label, sep, value = line.partition("：")
        if not sep:
            label, sep, value = line.partition(":")
        if not sep:
            continue
        label = label.strip()
        value = value.strip()
        if not value:
            continue

        if label == "评级":
            segments = [part.strip() for part in re.split(r"[/／]", value) if part.strip()]
            if segments:
                parsed["grade_interval"] = segments[0]
                match = _GRADE_RE.search(segments[0])
                if match:
                    parsed["grade_score"] = int(match.group(2))
            if len(segments) > 1:
                parsed["grade_cn"] = segments[1]
            if len(segments) > 2:
                parsed["color_grade"] = segments[2]
            if len(segments) > 3:
                parsed["net_grade"] = segments[3].removeprefix("净分").strip() or None
            continue

        field = _LABELS.get(label)
        if field:
            parsed[field] = value
    return parsed


def is_best(score: int | None, interval: str | None) -> bool:
    """好品推荐门槛：区间下界 >= 40 且前缀属于 XF / AU / MS。"""
    if score is None or score < 40:
        return False
    return _grade_prefix(interval) in _BEST_PREFIXES

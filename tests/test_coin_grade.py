"""铜币品相评级测试：只覆盖提示词组装与 CLI 参数解析，不发真实网络请求。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import cli
from app.coin_grade import parse, prompt
from app.coin_grade.mimo_client import MimoClient, MimoError
from app.models import CoinSeries


def _series(**overrides: object) -> CoinSeries:
    data: dict[str, object] = {
        "id": 1,
        "numista_id": 1234,
        "title": "1 Penny",
        "url": None,
        "object_type": "coin",
        "issuer": "United Kingdom",
        "min_year": 1900,
        "max_year": 1900,
        "ruler": None,
        "denomination": "1 penny",
        "size_mm": 30.8,
        "weight_g": 9.4,
        "thickness_mm": None,
        "shape": "round",
        "composition": "bronze",
        "edge": "plain",
        "obverse_desc": "bust facing left",
        "obverse_lettering": None,
        "obverse_picture": None,
        "reverse_desc": "Britannia seated",
        "reverse_lettering": None,
        "reverse_picture": None,
        "catalog_no": None,
        "comments": None,
        "mints": [],
        "fetched_at": "2026-11-11",
    }
    data.update(overrides)
    return CoinSeries(**data)  # type: ignore[arg-type]


def test_build_vision_prompt_fills_series_fields() -> None:
    text = prompt.build_vision_user_prompt(_series())

    assert "币种：United Kingdom/1900/1 penny" in text
    assert "正面描述：bust facing left" in text
    assert "背面描述：Britannia seated" in text


def test_build_vision_prompt_marks_missing_fields() -> None:
    text = prompt.build_vision_user_prompt(_series(issuer=None, composition=None, obverse_desc=""))

    assert "币种：未提供/1900/1 penny" in text
    assert "正面描述：未提供" in text


def test_grader_prompt_keeps_key_constraints() -> None:
    text = prompt.build_grader_user_prompt(_series(), "特征：高点轻微磨圆")

    for fragment in ("谢尔顿70分制", "合法刻度", "青铜病", "盲区", "锚点校验"):
        assert fragment in text
    assert "特征：高点轻微磨圆" in text
    assert "严禁跨大档" in prompt.GRADER_SYSTEM_PROMPT


def test_cli_parses_grade_features_args() -> None:
    args = cli._build_parser().parse_args(["coins-grade-features", "a.jpg", "b.jpg", "1234"])

    assert args.obverse == "a.jpg"
    assert args.reverse == "b.jpg"
    assert args.series_id == 1234
    assert args.func is cli.cmd_coins_grade_features


def test_cli_parses_grade_args() -> None:
    args = cli._build_parser().parse_args(["coins-grade", "4721"])

    assert args.feature_id == 4721
    assert args.func is cli.cmd_coins_grade


def test_user_content_passes_http_url_through() -> None:
    url = "https://oss.mxiqi.com/V1/AUC/20260918/IMG/aa60ea7a93e1d"
    parts = MimoClient._user_content("评级", [url, "https://oss.mxiqi.com/b.jpg"])

    assert parts[1] == {"type": "image_url", "image_url": {"url": url}}
    assert parts[2] == {
        "type": "image_url",
        "image_url": {"url": "https://oss.mxiqi.com/b.jpg"},
    }


def test_user_content_rejects_missing_local_file(tmp_path: Path) -> None:
    with pytest.raises(MimoError, match="图片不存在"):
        MimoClient._user_content("评级", [tmp_path / "nope.jpg"])


def test_parse_grade_splits_interval_colors_and_labels() -> None:
    parsed = parse.parse_grade(
        "评级：XF40-45 / 极美 / 深褐 / 净分40\n"
        "磨损：观察区1鼻尖为C级\n"
        "触发规则：冲突保底\n"
        "降级说明：纯磨损降级\n"
        "综合判定为纯磨损，由最差核心高点决定。\n"
        "瑕疵与盲区：边缘轻磕，微观盲区\n"
        "锚点校验：确认未扣分"
    )

    assert parsed["grade_interval"] == "XF40-45"
    assert parsed["grade_score"] == 40
    assert parsed["grade_cn"] == "极美"
    assert parsed["color_grade"] == "深褐"
    assert parsed["net_grade"] == "40"
    assert parsed["wear_desc"] == "观察区1鼻尖为C级"
    assert parsed["blind_spots"] == "边缘轻磕，微观盲区"


def test_parse_grade_keeps_missing_fields_empty() -> None:
    parsed = parse.parse_grade("评级：铜币\n磨损：轻微")

    assert parsed["grade_interval"] == "铜币"
    assert parsed["grade_score"] is None
    assert parsed["grade_cn"] is None
    assert parsed["color_grade"] is None
    assert parsed["wear_desc"] == "轻微"
    assert parsed["blind_spots"] is None

    assert parse.parse_grade(None)["grade_interval"] is None
    assert parse.parse_grade("")["grade_score"] is None


def test_is_best_requires_xf_prefix_and_lower_bound() -> None:
    assert parse.is_best(40, "XF40-45") is True
    assert parse.is_best(50, "AU50") is True
    assert parse.is_best(39, "XF39") is False
    assert parse.is_best(40, "VF40") is False
    assert parse.is_best(None, "XF40-45") is False

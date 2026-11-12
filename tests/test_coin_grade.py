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


def test_build_user_prompt_fills_series_fields() -> None:
    text = prompt.build_user_prompt(_series())

    assert "国家：United Kingdom" in text
    assert "年代：1900" in text
    assert "材质：bronze" in text
    assert "面值：1 penny" in text
    assert "正面图案描述：bust facing left" in text
    assert "背面图案描述：Britannia seated" in text


def test_build_user_prompt_marks_missing_fields() -> None:
    text = prompt.build_user_prompt(_series(issuer=None, composition=None, obverse_desc=""))

    assert "国家：未提供" in text
    assert "材质：未提供" in text
    assert "正面图案描述：未提供" in text


def test_system_prompt_keeps_key_constraints() -> None:
    for fragment in ("谢尔顿70分制", "青铜病", "合法刻度", "盲区", "置信度"):
        assert fragment in prompt.SYSTEM_PROMPT


def test_cli_parses_three_positional_args() -> None:
    args = cli._build_parser().parse_args(["coins-grade", "a.jpg", "b.jpg", "1234"])

    assert args.obverse == "a.jpg"
    assert args.reverse == "b.jpg"
    assert args.series_id == 1234
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
        "评级：XF40-45 / 极美 / 深铜 / 净分40\n"
        "磨损：高点轻微磨损\n"
        "铜色：深铜色均匀\n"
        "瑕疵：无明显瑕疵\n"
        "盲区：边缘\n"
        "置信度：0.8"
    )

    assert parsed["grade_interval"] == "XF40-45"
    assert parsed["grade_score"] == 40
    assert parsed["grade_cn"] == "极美"
    assert parsed["color_grade"] == "深铜"
    assert parsed["net_grade"] == "40"
    assert parsed["wear_desc"] == "高点轻微磨损"
    assert parsed["color_desc"] == "深铜色均匀"
    assert parsed["flaws"] == "无明显瑕疵"
    assert parsed["blind_spots"] == "边缘"
    assert parsed["confidence"] == "0.8"


def test_parse_grade_keeps_missing_fields_empty() -> None:
    parsed = parse.parse_grade("评级：铜币\n磨损：轻微")

    assert parsed["grade_interval"] == "铜币"
    assert parsed["grade_score"] is None
    assert parsed["grade_cn"] is None
    assert parsed["color_grade"] is None
    assert parsed["wear_desc"] == "轻微"
    assert parsed["flaws"] is None

    assert parse.parse_grade(None)["grade_interval"] is None
    assert parse.parse_grade("")["grade_score"] is None


def test_is_best_requires_xf_prefix_and_lower_bound() -> None:
    assert parse.is_best(40, "XF40-45") is True
    assert parse.is_best(50, "AU50") is True
    assert parse.is_best(39, "XF39") is False
    assert parse.is_best(40, "VF40") is False
    assert parse.is_best(None, "XF40-45") is False

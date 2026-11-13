"""好品推荐（11070 铜币品相评级）测试。

运行方式：`.venv/bin/python -m pytest tests/test_coins_best.py -q`
纯临时库 + 假模型客户端，不发网络请求。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import db, repository
from app.coins import best
from app.config import (
    AppConfig,
    CoinGradeConfig,
    CoinGradeStageConfig,
    Config,
    DigestConfig,
    FetchConfig,
    LLMConfig,
    ScoringConfig,
)
from app.models import CoinLot, CoinSeries, CoinSession

GRADE_TEXT = """评级：XF40-45 / 极美 / 深褐 / 净分40
磨损：观察区1鼻尖为C级
瑕疵与盲区：边缘轻磕"""

FEATURE_TEXT = "1. 核心高点状态：鼻尖轻微磨圆"


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "best.db")
    repository.upsert_coin_categories(
        [
            ("11070", "铜币", 1, None, 3, None, None, None),
            ("11010", "铜币", 1, None, 3, None, None, None),
            ("11030", "铜币", 1, None, 3, None, None, None),
            ("11060", "其他", 2, None, 3, None, None, None),
        ]
    )
    return tmp_path / "best.db"


@pytest.fixture()
def cfg(tmp_path):
    return Config(
        app=AppConfig(
            host="127.0.0.1",
            port=0,
            timezone="Asia/Shanghai",
            db_path=tmp_path / "best.db",
            log_path=tmp_path / "best.log",
        ),
        digest=DigestConfig(limit=1, lookback_hours=1),
        fetch=FetchConfig(
            timeout_seconds=5,
            user_agent="test",
            max_items_per_source=1,
            max_concurrency=1,
        ),
        scoring=ScoringConfig(
            concurrency=1,
            max_summary_chars=100,
            max_attempts=1,
            prompt_version="v1",
            temperature=0.0,
        ),
        llm=LLMConfig(
            base_url="",
            model="",
            api_key_env="DESKHUB_LLM_API_KEY",
            api_key="",
        ),
        modules={},
        sources=(),
        coin_grade=CoinGradeConfig(
            vision=CoinGradeStageConfig(
                base_url="http://vision.test",
                model="vision-test",
                api_key="k",
            ),
            reason=CoinGradeStageConfig(
                base_url="http://reason.test",
                model="reason-test",
                api_key="k",
            ),
        ),
    )


def _series(numista_id: int = 670) -> CoinSeries:
    return CoinSeries(
        id=None,
        numista_id=numista_id,
        title="英国 1 便士",
        url=None,
        object_type="标准流通币",
        issuer="英国",
        min_year=1860,
        max_year=1864,
        ruler="维多利亚",
        denomination="1 便士",
        size_mm=31.0,
        weight_g=9.4,
        thickness_mm=None,
        shape="圆形",
        composition="Bronze",
        edge=None,
        obverse_desc=None,
        obverse_lettering=None,
        obverse_picture=None,
        reverse_desc=None,
        reverse_lettering=None,
        reverse_picture=None,
        catalog_no=None,
        comments=None,
        mints=[],
        fetched_at=repository.now_local(),
    )


def _session(cfg: Config) -> int:
    auction_at = repository.to_local_str(
        datetime.now(cfg.app.tz).replace(hour=15, minute=25, second=0, microsecond=0)
    )
    return repository.upsert_coin_session(
        CoinSession(
            id=None,
            mxiqi_id="best-1",
            shop_id=None,
            title="东印度铜币专场",
            status="running",
            auction_at=auction_at,
            auction_at_raw="今天 15:25",
            deposit=None,
            lot_total=4,
            view_count=0,
            cover_urls=[],
            fetched_at=repository.now_local(),
        )
    )


def _lot(
    session_id: int,
    mxiqi_id: str,
    title: str,
    *,
    category_code: str | None = "11070",
    image_urls: tuple[str, ...] = ("https://img.example/1.png",),
    is_graded: int | None = None,
) -> int:
    return repository.upsert_coin_lot(
        CoinLot(
            id=None,
            session_id=session_id,
            mxiqi_id=mxiqi_id,
            lot_no=None,
            title=title,
            start_price=1.0,
            current_price=0.0,
            image_urls=list(image_urls),
            tags={},
            match_score=0.0,
            is_recommended=True,
            fetched_at=repository.now_local(),
            category_code=category_code,
            is_graded=is_graded,
        )
    )


class FakeClient:
    def __init__(self, text: str = GRADE_TEXT) -> None:
        self.text = text
        self.calls: list[list[object]] = []
        self.completed: list[str] = []

    def grade(self, *, system: str, user: str, images: list[object]) -> str:
        self.calls.append(list(images))
        return FEATURE_TEXT

    def complete(self, *, system: str, user: str) -> str:
        self.completed.append(user)
        return self.text

    def close(self) -> None:
        pass


def test_match_today_lots_matches_and_is_idempotent(temp_db, cfg):
    repository.upsert_coin_series_cn(_series())
    session_id = _session(cfg)
    _lot(session_id, "best-1:hit", "英国 1861年 1 便士 铜币")
    _lot(session_id, "best-1:year-out", "英国 1999年 1 便士")
    _lot(session_id, "best-1:other-category", "英国 1862年 1 便士", category_code="11060")

    assert best.match_today_lots(cfg) == 1
    assert len(repository.list_pending_grade_lots(10)) == 1

    assert best.match_today_lots(cfg) == 0
    assert len(repository.list_pending_grade_lots(10)) == 1


def test_match_today_lots_covers_extra_categories_and_skips_graded(temp_db, cfg):
    repository.upsert_coin_series_cn(_series())
    session_id = _session(cfg)
    _lot(session_id, "best-2:cat-11010", "英国 1861年 1 便士 铜币", category_code="11010")
    _lot(session_id, "best-2:cat-11030", "英国 1862年 1 便士 铜币", category_code="11030")
    _lot(session_id, "best-2:graded", "英国 1863年 1 便士 铜币", is_graded=1)
    _lot(session_id, "best-2:silver", "英国 1864年 1 便士 银币")
    _lot(session_id, "best-2:commemorative", "英国 1861年 1 便士 纪念币")

    assert best.match_today_lots(cfg) == 2
    assert len(repository.list_pending_grade_lots(10)) == 2


def test_grade_candidates_skips_missing_image_and_caps_daily_quota(
    temp_db, cfg, monkeypatch
):
    repository.upsert_coin_series_cn(_series())
    session_id = _session(cfg)
    _lot(session_id, "best-1:with-image", "英国 1861年 1 便士 铜币")
    _lot(session_id, "best-1:no-image", "英国 1862年 1 便士 铜币", image_urls=())
    assert best.match_today_lots(cfg) == 2

    client = FakeClient()
    assert best.grade_candidates(cfg, vision_client=client, reason_client=client) == 1
    assert len(client.calls) == 1
    assert len(client.completed) == 1
    assert FEATURE_TEXT in client.completed[0]
    assert repository.count_coin_graded_on_day() == 1

    items = best.load_best_lots(cfg)
    assert [item.grade_interval for item in items] == ["XF40-45"]
    assert [item.grade_score for item in items] == [40]

    monkeypatch.setattr(
        repository,
        "count_coin_graded_on_day",
        lambda day=None: best.BEST_DAILY_GRADE_LIMIT,
    )
    _lot(session_id, "best-1:quota-full", "英国 1863年 1 便士 铜币")
    assert best.match_today_lots(cfg) == 1
    assert best.grade_candidates(cfg, vision_client=client, reason_client=client) == 0
    assert len(client.calls) == 1

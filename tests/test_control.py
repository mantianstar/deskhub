"""运行控制页与功能开关（plan §19）。

运行方式：`.venv/bin/python -m pytest tests/test_control.py -q`
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, repository, switches, tasks
from app.main import app as fastapi_app


@pytest.fixture()
def web(tmp_path, monkeypatch, no_scheduler):
    """走真实 lifespan（建库 + 同步源清单），调度器走空壳。"""
    monkeypatch.setenv("DESKHUB_DB_PATH", str(tmp_path / "control.db"))
    config.reset()
    with TestClient(fastapi_app) as client:
        yield client
    config.reset()


@pytest.fixture()
def clean_task_state(monkeypatch):
    """手动运行的状态是模块级全局，测试之间要隔离。"""
    monkeypatch.setattr(tasks, "_running", {})
    monkeypatch.setattr(tasks, "_last_state", {})


def _wait_for_state(key: str, timeout: float = 5.0) -> tasks.RunState | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = tasks.last_state(key)
        if state is not None:
            return state
        time.sleep(0.05)
    return None


# ---------------------------------------------------------------- 开关读写


def test_all_features_enabled_by_default(tmp_path):
    """没记录过的功能按默认启用：重启不再跑任务，但 08:00 的定时任务保持原行为。"""
    db.init_db(tmp_path / "switches.db")
    assert [feature.key for feature in switches.FEATURES] == [
        switches.KNOWLEDGE_FETCH,
        switches.KNOWLEDGE_SCORE,
        switches.COINS_COLLECT,
        switches.COINS_FINALIZE,
        switches.COINS_MATCH,
        switches.COINS_PRICE,
    ]
    for feature in switches.FEATURES:
        assert switches.is_enabled(feature.key) is True


def test_toggle_persists_to_db(tmp_path):
    """关掉一个功能会落库（不是内存态）：重新读出来仍是关的 —— 重启后保留。"""
    db.init_db(tmp_path / "switches.db")
    assert switches.toggle(switches.COINS_PRICE) is False
    assert repository.feature_switch_states() == {switches.COINS_PRICE: False}
    assert switches.is_enabled(switches.COINS_PRICE) is False
    assert switches.is_enabled(switches.KNOWLEDGE_FETCH) is True


def test_unknown_key_counts_as_disabled(tmp_path):
    """未知 key 当未启用：拼错 key 只会不跑，不会静默跑起来。"""
    db.init_db(tmp_path / "switches.db")
    assert switches.is_enabled("nope") is False
    with pytest.raises(ValueError):
        switches.toggle("nope")


# ---------------------------------------------------------------- 页面与动作


def test_control_page_lists_all_features(web):
    resp = web.get("/control")
    assert resp.status_code == 200
    for feature in switches.FEATURES:
        assert feature.label in resp.text
        assert feature.group in resp.text


def test_match_is_manual_only(web):
    """「匹配」只给立即运行：标「仅手动」，不渲染启停按钮。"""
    text = web.get("/control").text
    assert "仅手动" in text
    assert f"/control/{switches.COINS_MATCH}/toggle" not in text
    assert f"/control/{switches.COINS_MATCH}/run" in text


def test_toggle_endpoint_flips_and_redirects(web):
    """启停按钮：302 回本页，状态翻转并落库。"""
    url = f"/control/{switches.COINS_COLLECT}/toggle"
    assert web.post(url, follow_redirects=False).status_code == 302
    assert switches.is_enabled(switches.COINS_COLLECT) is False
    web.post(url)
    assert switches.is_enabled(switches.COINS_COLLECT) is True


def test_unknown_feature_returns_404(web):
    assert web.post("/control/nope/toggle").status_code == 404
    assert web.post("/control/nope/run").status_code == 404


def test_run_ignores_switch_and_finishes_in_background(web, monkeypatch, clean_task_state):
    """「立即运行」忽略开关、后台跑完，并把结果留在页面上。"""

    async def fake(config) -> str:
        return "假装跑完了"

    monkeypatch.setitem(tasks.RUNNERS, switches.COINS_COLLECT, fake)

    web.post(f"/control/{switches.COINS_COLLECT}/toggle")  # 先把功能关掉
    assert switches.is_enabled(switches.COINS_COLLECT) is False

    resp = web.post(f"/control/{switches.COINS_COLLECT}/run", follow_redirects=False)
    assert resp.status_code == 302

    state = _wait_for_state(switches.COINS_COLLECT)
    assert state is not None, "后台任务没有跑起来"
    assert state.status == "ok"
    assert state.message == "假装跑完了"
    assert "假装跑完了" in web.get("/control").text

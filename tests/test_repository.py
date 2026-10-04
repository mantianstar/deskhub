"""数据层测试：用临时 db 文件跑真实 SQL（plan §13.1）。

运行方式：`.venv/bin/python -m pytest tests/test_repository.py -q`
（用 `-m pytest` 而不是 `pytest`，保证项目根目录在 sys.path 上。）
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from app import db, models, repository
from app.config import SourceConfig
from app.models import FetchStatus

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 统一时间口径，方便断言「失败不动 last_ok_at」这类差异
FIRST_AT = "2026-09-22T00:00:01Z"


@pytest.fixture()
def temp_db(tmp_path):
    """每个用例一个独立库文件，避免用例间互相污染。"""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _source(name: str, url: str, module: str = "agent", enabled: bool = True) -> SourceConfig:
    return SourceConfig(name=name, url=url, module=module, enabled=enabled)


# ---------------------------------------------------------------- 脚手架


def _hash(url: str) -> str:
    """`repository` 只依赖 `url_hash` 的唯一性，不关心它怎么算 —— 同一 url 必须算出同一个值。"""
    return "hash::" + url


def _item(source_id: int, url: str, *, fetched_at: str = FIRST_AT) -> models.Item:
    return models.Item(
        id=None,
        source_id=source_id,
        module="agent",
        title="测试条目",
        url=url,
        url_hash=_hash(url),
        published_at=None,
        summary=None,
        fetched_at=fetched_at,
    )


def _run(
    source_id: int,
    status: str,
    *,
    finished_at: str = FIRST_AT,
    http_status: int | None = None,
    error: str | None = None,
) -> models.FetchRun:
    return models.FetchRun(
        id=None,
        source_id=source_id,
        started_at="2026-09-22T00:00:00Z",
        finished_at=finished_at,
        status=status,
        http_status=http_status,
        items_new=0,
        error=error,
    )


def _prepare(url: str = "https://www.infoq.cn/feed") -> int:
    """同步一个源，返回它的 id。"""
    repository.sync_sources_from_config((_source("InfoQ 中文", url),))
    return repository.list_sources()[0].id


def _count(table: str) -> int:
    with db.connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


# ---------------------------------------------------------------- 源清单同步


def test_sync_disables_sources_removed_from_config(temp_db):
    """配置里删掉的源只置 enabled=0，行保留（否则会级联删掉 items / fetch_runs 历史）。"""
    hn = _source("Hacker News", "https://news.ycombinator.com/rss")
    infoq = _source("InfoQ 中文", "https://www.infoq.cn/feed")
    repository.sync_sources_from_config((hn, infoq))
    assert [s.name for s in repository.list_sources(only_enabled=True)] == ["Hacker News", "InfoQ 中文"]

    # 源清单换成纯中文源后再次 sync
    repository.sync_sources_from_config((infoq,))

    assert repository.count_sources() == 2
    assert [s.name for s in repository.list_sources(only_enabled=True)] == ["InfoQ 中文"]


def test_sync_preserves_runtime_state(temp_db):
    """重复 sync 不增行，且 last_ok_at / fail_count 不被配置文件覆盖。"""
    config = (_source("InfoQ 中文", "https://www.infoq.cn/feed"),)
    repository.sync_sources_from_config(config)
    with db.connect() as conn, conn:
        conn.execute(
            "UPDATE sources SET last_ok_at = ?, fail_count = 2 WHERE url = ?",
            ("2026-09-21T00:00:00Z", "https://www.infoq.cn/feed"),
        )

    repository.sync_sources_from_config(config)

    assert repository.count_sources() == 1
    source = repository.list_sources()[0]
    assert source.last_ok_at == "2026-09-21T00:00:00Z"
    assert source.fail_count == 2


def test_config_enabled_overrides_db_toggle(temp_db):
    """配置是源清单唯一真源：界面临时停用的源在下次 sync 后按配置恢复。"""
    config = (_source("InfoQ 中文", "https://www.infoq.cn/feed"),)
    repository.sync_sources_from_config(config)
    source_id = repository.list_sources()[0].id
    repository.set_source_enabled(source_id, False)
    assert repository.list_sources(only_enabled=True) == []

    repository.sync_sources_from_config(config)

    assert [s.id for s in repository.list_sources(only_enabled=True)] == [source_id]


# ---------------------------------------------------------------- 幂等写入与源状态机


def test_same_feed_twice_inserts_items_once(temp_db):
    """同一 feed 抓两次：第二次 items_new=0、items 行数不变（§13.1 用例 2）。"""
    source_id = _prepare()
    first = repository.save_fetch_result(
        source_id, [_item(source_id, "https://a.com/1")], _run(source_id, FetchStatus.OK)
    )
    second = repository.save_fetch_result(
        source_id, [_item(source_id, "https://a.com/1")], _run(source_id, FetchStatus.OK)
    )

    assert (first, second) == (1, 0)
    assert _count("items") == 1
    assert _count("fetch_runs") == 2  # 抓取本身记两次，去重只作用在条目上


def test_http_error_bumps_fail_count_and_keeps_last_ok_at(temp_db):
    """源返回 500：fail_count=1、last_ok_at 不变，失败原因可追溯（§13.1 用例 3）。"""
    source_id = _prepare()
    repository.save_fetch_result(source_id, [], _run(source_id, FetchStatus.OK))

    repository.save_fetch_result(
        source_id,
        [],
        _run(
            source_id,
            FetchStatus.HTTP_ERROR,
            finished_at="2026-09-23T00:00:01Z",
            http_status=500,
            error="HTTP 500",
        ),
    )

    source = repository.list_sources()[0]
    assert source.fail_count == 1
    assert source.last_ok_at == FIRST_AT  # 失败不动它
    run = repository.latest_fetch_runs()[source_id]
    assert (run.status, run.http_status, run.error) == ("http_error", 500, "HTTP 500")


def test_empty_feed_touches_last_ok_at_but_keeps_fail_count(temp_db):
    """合法 feed 但 0 条：算源是活的（更新 last_ok_at），但不洗掉连败记录（§13.1 用例 4）。"""
    source_id = _prepare()
    repository.save_fetch_result(
        source_id, [], _run(source_id, FetchStatus.TIMEOUT, error="ReadTimeout")
    )
    repository.save_fetch_result(
        source_id,
        [],
        _run(source_id, FetchStatus.TIMEOUT, finished_at="2026-09-22T00:00:02Z", error="ReadTimeout"),
    )
    assert repository.list_sources()[0].fail_count == 2

    repository.save_fetch_result(
        source_id, [], _run(source_id, FetchStatus.EMPTY, finished_at="2026-09-22T00:00:03Z")
    )

    source = repository.list_sources()[0]
    assert source.fail_count == 2
    assert source.last_ok_at == "2026-09-22T00:00:03Z"


def test_success_after_three_failures_resets_fail_count(temp_db):
    """连败 3 次即达标红阈值，成功一次后归零（§13.1 用例 5）。"""
    source_id = _prepare()
    for index in range(3):
        repository.save_fetch_result(
            source_id,
            [],
            _run(source_id, FetchStatus.HTTP_ERROR, finished_at=f"2026-09-22T00:00:0{index}Z"),
        )
    assert repository.count_failing_sources() == 1

    repository.save_fetch_result(
        source_id, [], _run(source_id, FetchStatus.OK, finished_at="2026-09-22T00:01:00Z")
    )

    assert repository.list_sources()[0].fail_count == 0
    assert repository.count_failing_sources() == 0


# ---------------------------------------------------------------- 清理（cli purge）


def test_purge_deletes_older_items_and_cascades_scores(temp_db):
    """purge 只删 fetched_at 之前的条目，item_scores 随外键级联，源与抓取历史保留。"""
    source_id = _prepare()
    old_id = repository.insert_item_if_new(
        _item(source_id, "https://a.com/old", fetched_at="2026-09-21T00:00:00Z")
    )
    new_id = repository.insert_item_if_new(
        _item(source_id, "https://a.com/new", fetched_at="2026-09-22T00:00:00Z")
    )
    repository.upsert_score(old_id, 60, "旧条目的理由，十个字以上", "test-model", "v1")
    repository.upsert_score(new_id, 70, "新条目的理由，十个字以上", "test-model", "v1")
    repository.record_fetch_run(_run(source_id, FetchStatus.OK))

    # 业务时区的「09-22 00:00」= UTC 09-21 16:00，删的是它之前抓到的
    cutoff = repository.day_bounds_utc(SHANGHAI, date(2026, 9, 22))[0]
    assert cutoff == "2026-09-21T16:00:00Z"
    assert repository.count_items_before(cutoff) == 1

    assert repository.purge_items_before(cutoff) == 1

    assert _count("items") == 1
    assert _count("item_scores") == 1  # 只带走旧条目那条打分
    assert repository.get_item_url(old_id) is None
    assert repository.get_item_url(new_id) == "https://a.com/new"
    assert _count("sources") == 1
    assert _count("fetch_runs") == 1


def test_purge_preview_counts_without_deleting(temp_db):
    """不带 --yes 时只数不删：全量保留才是默认策略，purge 只手工触发。"""
    source_id = _prepare()
    repository.insert_item_if_new(
        _item(source_id, "https://a.com/old", fetched_at="2026-09-20T00:00:00Z")
    )

    cutoff = repository.day_bounds_utc(SHANGHAI, date(2026, 9, 22))[0]

    assert repository.count_items_before(cutoff) == 1
    assert _count("items") == 1

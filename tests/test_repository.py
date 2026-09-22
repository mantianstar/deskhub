"""数据层测试：用临时 db 文件跑真实 SQL（plan §13.1）。

运行方式：`.venv/bin/python -m pytest tests/test_repository.py -q`
（用 `-m pytest` 而不是 `pytest`，保证项目根目录在 sys.path 上。）
"""

from __future__ import annotations

import pytest

from app import db, repository
from app.config import SourceConfig


@pytest.fixture()
def temp_db(tmp_path):
    """每个用例一个独立库文件，避免用例间互相污染。"""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _source(name: str, url: str, module: str = "agent", enabled: bool = True) -> SourceConfig:
    return SourceConfig(name=name, url=url, module=module, enabled=enabled)


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

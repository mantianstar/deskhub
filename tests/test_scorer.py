"""打分测试：注入 FakeClient，全程不访问真实 LLM（plan §13.1 用例 6/7）。

跑法：`.venv/bin/python -m pytest tests/test_scorer.py -q`

重点覆盖两件事：**输出校验的边界**（坏 JSON / clamp / reason 过短）和
**失败不落库**（宁可这条不打分，也不要落一条空话）。
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db, fetcher, llm, repository, scorer
from app.config import (
    AppConfig,
    Config,
    DigestConfig,
    FetchConfig,
    LLMConfig,
    ModuleConfig,
    ScoringConfig,
    SourceConfig,
)
from app.models import Item

SOURCE_URL = "https://example.com/feed"
GOOD_REASON = "讲了 Flink 状态后端从 RocksDB 迁到本地盘的具体步骤和踩坑，和你正在做的实时计算直接相关"
GOOD_JSON = f'{{"score": 88, "reason": "{GOOD_REASON}"}}'

_url_seq = itertools.count(1)


# ---------------------------------------------------------------- 脚手架


def _config(*sources: SourceConfig, api_key: str = "test-key", max_attempts: int = 2) -> Config:
    return Config(
        app=AppConfig(
            host="127.0.0.1",
            port=8765,
            timezone="Asia/Shanghai",
            db_path=Path("data/test.db"),
            log_path=Path("data/logs/test.log"),
        ),
        digest=DigestConfig(limit=8, lookback_hours=48),
        fetch=FetchConfig(
            timeout_seconds=10,
            user_agent="deskhub-test/1.0",
            max_items_per_source=30,
            max_concurrency=1,
        ),
        scoring=ScoringConfig(
            concurrency=3,
            max_summary_chars=1500,
            max_attempts=max_attempts,
            prompt_version="v1",
            temperature=0.2,
        ),
        llm=LLMConfig(
            base_url="https://api.example.com/v1",
            model="test-model",
            api_key_env="DESKHUB_TEST_KEY",
            api_key=api_key,
        ),
        modules={
            "agent": ModuleConfig(key="agent", label="Agent 开发", focus="Agent 方向"),
            "bigdata": ModuleConfig(key="bigdata", label="大数据", focus="大数据方向"),
        },
        sources=sources,
    )


def _prepare(tmp_path: Path, *sources: SourceConfig, **kwargs) -> Config:
    db.init_db(tmp_path / "test.db")
    config = _config(*sources, **kwargs)
    repository.sync_sources_from_config(config.sources)
    return config


def _source(module: str = "agent") -> SourceConfig:
    return SourceConfig(name="测试源", url=SOURCE_URL, module=module, enabled=True)


def _add_item(
    title: str = "Flink 状态后端迁移",
    *,
    module: str = "agent",
    summary: str | None = "正文摘要",
    published_at: str | None = None,
    fetched_at: str | None = None,
    url: str | None = None,
) -> int:
    link = url or f"https://example.com/item-{next(_url_seq)}"
    source = repository.list_sources(only_enabled=True)[0]
    item_id = repository.insert_item_if_new(
        Item(
            id=None,
            source_id=source.id,
            module=module,
            title=title,
            url=link,
            url_hash=fetcher.url_hash(link),
            published_at=published_at,
            summary=summary,
            fetched_at=fetched_at or repository.utcnow(),
        )
    )
    assert item_id is not None
    return item_id


def _score_row(item_id: int):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM item_scores WHERE item_id = ?", (item_id,)).fetchone()


# ---------------------------------------------------------------- prompt 组装


def test_prompt_matches_plan_and_truncates_summary(tmp_path):
    """system 含 module 关注点与三段口径，user 含标题/来源/时间/截断后的摘要（plan §8.2）。"""
    config = _prepare(tmp_path, _source(module="bigdata"))
    item_id = _add_item(
        title="Flink 1.20 状态后端调优",
        summary="A" * 5000,
        published_at="2026-09-20T10:00:00Z",
        module="bigdata",
    )
    candidate = repository.list_pending_score_items(10)[0]

    system, user = scorer.build_prompt(config, config.module("bigdata"), candidate)

    assert "大数据" in system and "大数据方向" in system
    assert "相关度 0-50" in system and "信息密度 0-30" in system and "时效性 0-20" in system
    assert "禁止出现「内容优质」" in system
    assert "标题：Flink 1.20 状态后端调优" in user
    assert "来源：测试源" in user
    assert "发布时间：2026-09-20T10:00:00Z" in user
    # 摘要按 scoring.max_summary_chars 截断，不整篇塞给模型
    assert f"摘要：{'A' * config.scoring.max_summary_chars}" in user
    assert "A" * (config.scoring.max_summary_chars + 1) not in user
    assert candidate.item_id == item_id


def test_prompt_handles_missing_summary_and_published_at(tmp_path):
    """无摘要 / 无发布时间要给明确提示，而不是留空（plan §8.2）。"""
    config = _prepare(tmp_path, _source())
    _add_item(summary=None, published_at=None)
    candidate = repository.list_pending_score_items(10)[0]

    _, user = scorer.build_prompt(config, config.module("agent"), candidate)

    assert "发布时间：未知" in user
    assert scorer.NO_SUMMARY_HINT in user


# ---------------------------------------------------------------- 输出校验


@pytest.mark.parametrize(
    ("raw_score", "expected"),
    [("150", 100.0), ("-20", 0.0), ('"95"', 95.0), ("88.5", 88.5)],
)
def test_score_is_coerced_and_clamped(raw_score, expected):
    """`150` → 100、`"95"` → 95（plan §13.1 用例 7）。"""
    parsed = scorer.parse_score(f'{{"score": {raw_score}, "reason": "{GOOD_REASON}"}}')
    assert parsed.score == expected


@pytest.mark.parametrize(
    "raw",
    [
        "不是 JSON，只是在聊天",
        "{坏的 json",
        '{"reason": "' + GOOD_REASON + '"}',  # 缺 score
        '{"score": "abc", "reason": "' + GOOD_REASON + '"}',  # score 非数字
        '{"score": 80, "reason": "很好"}',  # reason 过短
        '{"score": 80}',  # 缺 reason
    ],
)
def test_invalid_output_raises(raw):
    with pytest.raises(ValueError):
        scorer.parse_score(raw)


def test_fenced_json_and_extra_text_is_parsed():
    """剥 ``` 围栏、取第一个 `{...}`：模型多嘴也能解析（plan §8.3）。"""
    raw = f"好的，结果如下：\n```json\n{GOOD_JSON}\n```\n希望有帮助"
    parsed = scorer.parse_score(raw)
    assert parsed.score == 88.0
    assert parsed.reason == GOOD_REASON


# ---------------------------------------------------------------- 重试与兜底


@pytest.mark.asyncio
async def test_bad_json_then_good_scores_on_retry(tmp_path):
    """第一次坏 JSON、第二次好：该条正常落库，且重试带了「只输出 JSON」。"""
    config = _prepare(tmp_path, _source())
    item_id = _add_item()
    client = llm.FakeClient(["这不是 JSON", GOOD_JSON])

    result = await scorer.run_pending(config, client=client)

    assert result.items_scored == 1
    assert result.items_failed == 0
    assert len(client.calls) == 2
    assert scorer.RETRY_HINT in client.calls[1][1]
    assert scorer.RETRY_HINT not in client.calls[0][1]
    row = _score_row(item_id)
    assert row["score"] == 88.0
    assert row["reason"] == GOOD_REASON
    assert row["prompt_version"] == "v1"
    assert row["model"] == "test-model"


@pytest.mark.asyncio
async def test_all_attempts_bad_json_leaves_item_pending(tmp_path):
    """重试仍坏：不写 item_scores，条目留在待打分池（plan §13.1 用例 6）。"""
    config = _prepare(tmp_path, _source())
    _add_item()
    client = llm.FakeClient(["坏", "还是坏"])

    result = await scorer.run_pending(config, client=client)

    assert (result.items_scored, result.items_failed) == (0, 1)
    assert repository.count_scored_items() == 0
    assert len(repository.list_pending_score_items(10)) == 1


@pytest.mark.asyncio
async def test_llm_error_does_not_write_score(tmp_path):
    """LLM 报错（HTTP 5xx）同样只算单条失败，不落库。"""
    config = _prepare(tmp_path, _source())
    _add_item()
    client = llm.FakeClient([llm.LLMError("HTTP 500"), llm.LLMError("HTTP 500")])

    result = await scorer.run_pending(config, client=client)

    assert (result.items_scored, result.items_failed) == (0, 1)
    assert repository.count_scored_items() == 0


@pytest.mark.asyncio
async def test_one_bad_item_does_not_block_others(tmp_path):
    """并发下每条互不影响：坏的那条失败，好的照常落库。"""
    config = _prepare(tmp_path, _source())
    bad_id = _add_item(title="坏条目", url="https://example.com/bad")
    good_id = _add_item(title="好条目", url="https://example.com/good")
    # 池子按 fetched_at DESC、同秒再按 id DESC 排，所以好条目先被处理：
    # 它一次成功，坏条目把两次重试额度用光。
    client = llm.FakeClient([GOOD_JSON, "坏", "坏"])

    result = await scorer.run_pending(config, client=client)

    assert result.pending == 2
    assert (result.items_scored, result.items_failed) == (1, 1)
    assert _score_row(good_id)["score"] == 88.0
    assert _score_row(bad_id) is None


@pytest.mark.asyncio
async def test_concurrency_is_capped_by_config(tmp_path):
    """并发上限由 `scoring.concurrency` 控制，不是把整池条目一次性打出去（M2-5）。"""

    class Probe:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.usage = llm.Usage()

        async def complete_json(self, system: str, user: str) -> str:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.02)
                return GOOD_JSON
            finally:
                self.active -= 1

        async def aclose(self) -> None:
            return None

    config = _prepare(tmp_path, _source())
    for index in range(6):
        _add_item(title=f"条目 {index}", url=f"https://example.com/concurrency-{index}")
    probe = Probe()

    result = await scorer.run_pending(config, client=probe)

    assert result.items_scored == 6
    assert probe.max_active == config.scoring.concurrency == 3


@pytest.mark.asyncio
async def test_missing_api_key_fails_fast(tmp_path):
    """没配 key 时不拿空 key 去撞 401（plan §5.3 允许抓取先行、打分后补）。"""
    config = _prepare(tmp_path, _source(), api_key="")
    _add_item()

    with pytest.raises(llm.LLMError):
        await scorer.run_pending(config)


@pytest.mark.asyncio
async def test_empty_pool_returns_zero_and_no_calls(tmp_path):
    config = _prepare(tmp_path, _source())
    client = llm.FakeClient([])

    result = await scorer.run_pending(config, client=client)

    assert (result.pending, result.items_scored, result.items_failed) == (0, 0, 0)
    assert client.calls == []
    assert result.total_tokens == 0


# ---------------------------------------------------------------- 待打分池与重跑


def test_pending_pool_excludes_scored_and_outdated_items(tmp_path):
    """已打分的不再入池；超出 lookback_days 的也不再送模型（plan §8.4）。"""
    _prepare(tmp_path, _source())
    fresh_id = _add_item(title="新条目", url="https://example.com/fresh")
    stale_id = _add_item(
        title="老条目",
        url="https://example.com/stale",
        fetched_at=repository.to_utc_str(datetime.now(timezone.utc) - timedelta(days=9)),
    )
    repository.upsert_score(fresh_id, 90.0, GOOD_REASON, "test-model", "v1")

    pending = repository.list_pending_score_items(10)

    assert [item.item_id for item in pending] == []
    assert repository.count_scored_items() == 1
    # 把窗口放宽到 10 天，老条目才回到池子里 —— 证明过滤的是 fetch 时间而不是别的
    assert [item.item_id for item in repository.list_pending_score_items(10, lookback_days=10)] == [
        stale_id
    ]


@pytest.mark.asyncio
async def test_rescore_updates_in_place_without_duplicate_rows(tmp_path):
    """重跑同一条：`item_scores` 不出现重复行，只有一份最新理由（M2-6）。"""
    config = _prepare(tmp_path, _source())
    item_id = _add_item()
    await scorer.run_pending(config, client=llm.FakeClient([GOOD_JSON]))

    # 已打分的不再入池
    assert repository.list_pending_score_items(10) == []
    # 同一条再写一次（重跑），靠 item_id 主键覆盖，行数不变
    repository.upsert_score(item_id, 40.0, "换了个理由：这篇只讲了概念，没有可复现的做法", "m", "v1")
    assert repository.count_scored_items() == 1
    assert _score_row(item_id)["score"] == 40.0

    # --rescore 的路径：删掉该版本 → 重新入池 → 重跑后仍只有一行
    assert repository.delete_scores_by_prompt_version("v1") == 1
    result = await scorer.run_pending(config, client=llm.FakeClient([GOOD_JSON]))

    assert result.items_scored == 1
    assert repository.count_scored_items() == 1
    assert _score_row(item_id)["score"] == 88.0

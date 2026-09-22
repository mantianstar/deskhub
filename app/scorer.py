"""LLM 打分：prompt 组装 → 调用 → 输出校验 → 落库（plan §8）。

`reason` 是本产品的核心资产（spec §7 要求「具体、可反驳」），所以这里的原则是
**宁可这条不打分，也不要落一条空话**：任何一步校验失败都不写 `item_scores`，
条目留在待打分池里下轮自然重试。

不依赖 FastAPI、不感知请求上下文，CLI 与调度器复用同一条管道。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from typing import Protocol

from app import llm, repository
from app.config import Config, ModuleConfig
from app.models import ScoreCandidate

logger = logging.getLogger(__name__)

# 待打分池默认取多少条（plan §8.4）
DEFAULT_LIMIT = 200
# reason 的硬下限：短于这个字数的理由必然是空话（plan §8.3）
MIN_REASON_CHARS = 10
# 重试时附加在 user 末尾的强约束（plan §8.3）
RETRY_HINT = "只输出 JSON"

SYSTEM_PROMPT_TEMPLATE = """你是信息筛选助手，只输出 JSON，不要任何解释文字。

请评估下面这条内容对「{module_label}」方向读者的价值。
该方向关注：{module_focus}

打分口径（总分 100）：
- 相关度 0-50：与上述关注点的直接相关程度。泛泛的行业融资新闻最多 15 分。
- 信息密度 0-30：是否给出具体方法、数据、架构、可复现结论。纯观点/营销稿最多 10 分。
- 时效性 0-20：越新越高。超过 30 天的内容最多 8 分。

不要因为来源知名而加分。宁可给低分，也不要为了填满而抬分。

reason 要求：20-60 字，必须具体——说清「这条讲了什么 + 为什么和读者相关」。
禁止出现「内容优质」「值得一读」「干货满满」这类空话，禁止复述标题。

输出格式：{{"score": <0-100 的整数>, "reason": "<中文一句话>"}}"""

USER_PROMPT_TEMPLATE = """标题：{title}
来源：{source_name}
发布时间：{published_at}
摘要：{summary}"""

NO_SUMMARY_HINT = "（无摘要，请仅依据标题判断，并相应降低信息密度分）"
UNKNOWN_PUBLISHED_AT = "未知"


class ScoreError(Exception):
    """单条条目打分失败（已用完重试次数）。"""


@dataclass(frozen=True)
class Score:
    score: float
    reason: str


@dataclass(frozen=True)
class ScoreRunResult:
    """一轮打分的汇总，供 CLI 打印与成本核对（plan §8.4）。"""

    pending: int
    items_scored: int
    items_failed: int
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ---------------------------------------------------------------- prompt 组装


def build_prompt(
    config: Config,
    module: ModuleConfig,
    candidate: ScoreCandidate,
) -> tuple[str, str]:
    """组装 prompt v1（plan §8.2）；与模型看到的完全一致，便于校准比对。"""
    summary = (candidate.summary or "").strip()
    if summary:
        summary = summary[: config.scoring.max_summary_chars]
    else:
        summary = NO_SUMMARY_HINT

    system = SYSTEM_PROMPT_TEMPLATE.format(
        module_label=module.label,
        module_focus=module.focus,
    )
    user = USER_PROMPT_TEMPLATE.format(
        title=candidate.title,
        source_name=candidate.source_name,
        published_at=candidate.published_at or UNKNOWN_PUBLISHED_AT,
        summary=summary,
    )
    return system, user


# ---------------------------------------------------------------- 输出校验


def _extract_json_object(text: str) -> str:
    """剥掉 ``` 围栏、取第一个 `{...}`（plan §8.3）。"""
    cleaned = text.replace("```json", "").replace("```", "")
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"响应里找不到 JSON 对象：{text.strip()[:120]!r}")
    return cleaned[start : end + 1]


def parse_score(text: str) -> Score:
    """把模型输出解析成 `Score`；任何不合格的地方都抛 `ValueError`。

    `score` 转 float 后 clamp 到 [0,100]（`150` → 100、`"95"` → 95）；
    `reason` 去空白后短于 `MIN_REASON_CHARS` 视为失败。
    """
    payload = json.loads(_extract_json_object(text))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层不是对象：{payload!r}")

    raw_score = payload.get("score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float, str)):
        raise ValueError(f"score 不是数字：{raw_score!r}")
    try:
        score = float(str(raw_score).strip())
    except ValueError:
        raise ValueError(f"score 无法转成数字：{raw_score!r}") from None
    if math.isnan(score) or math.isinf(score):
        raise ValueError(f"score 不是有限数：{raw_score!r}")
    score = min(100.0, max(0.0, score))

    reason = str(payload.get("reason") or "").strip()
    if len(reason) < MIN_REASON_CHARS:
        raise ValueError(f"reason 过短（{len(reason)} 字，至少 {MIN_REASON_CHARS} 字）：{reason!r}")

    return Score(score=score, reason=reason)


# ---------------------------------------------------------------- 单条打分


class _Client(Protocol):
    async def complete_json(self, system: str, user: str) -> str: ...


async def score_one(
    client: _Client,
    config: Config,
    module: ModuleConfig,
    candidate: ScoreCandidate,
) -> Score:
    """打一条分：失败重试，重试时附加「只输出 JSON」；用完次数仍失败抛 `ScoreError`。"""
    system, user = build_prompt(config, module, candidate)
    attempts = config.scoring.max_attempts
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        prompt = user if attempt == 1 else f"{user}\n\n{RETRY_HINT}"
        try:
            return parse_score(await client.complete_json(system, prompt))
        except (llm.LLMError, ValueError) as exc:
            last_error = exc
            logger.warning(
                "打分失败（第 %d/%d 次）item_id=%s：%s",
                attempt,
                attempts,
                candidate.item_id,
                exc,
            )
    raise ScoreError(f"重试 {attempts} 次仍失败：{last_error}") from last_error


# ---------------------------------------------------------------- 一轮打分


async def run_pending(
    config: Config,
    *,
    limit: int = DEFAULT_LIMIT,
    client: llm.LLMClient | None = None,
) -> ScoreRunResult:
    """给待打分池里的条目打分，返回汇总。

    - 并发由 `scoring.concurrency` 控制（v1 为 3），每条独立重试、互不影响。
    - 打分失败的条目不写库，留在池子里下轮重试（plan §8.3）。
    - `client` 传 None 时按配置造真实客户端；测试注入 `FakeClient`。
    """
    owned_client = client is None
    active = client or llm.get_client(config)

    try:
        pending = await asyncio.to_thread(repository.list_pending_score_items, limit)
        semaphore = asyncio.Semaphore(config.scoring.concurrency)

        async def handle(candidate: ScoreCandidate) -> bool:
            async with semaphore:
                try:
                    score = await score_one(
                        active, config, config.module(candidate.module), candidate
                    )
                except ScoreError as exc:
                    logger.warning(
                        "条目打分失败，保留在待打分池下轮重试 item_id=%s title=%s：%s",
                        candidate.item_id,
                        candidate.title,
                        exc,
                    )
                    return False
                # sqlite3 是同步库，丢线程里跑，别阻塞事件循环（plan §6.3）
                await asyncio.to_thread(
                    repository.upsert_score,
                    candidate.item_id,
                    score.score,
                    score.reason,
                    config.llm.model,
                    config.scoring.prompt_version,
                )
                logger.info(
                    "打分完成 item_id=%s score=%g reason=%s",
                    candidate.item_id,
                    score.score,
                    score.reason,
                )
                return True

        succeeded = (
            list(await asyncio.gather(*(handle(item) for item in pending))) if pending else []
        )
        items_scored = sum(1 for ok in succeeded if ok)
        result = ScoreRunResult(
            pending=len(pending),
            items_scored=items_scored,
            items_failed=len(succeeded) - items_scored,
            prompt_tokens=active.usage.prompt_tokens,
            completion_tokens=active.usage.completion_tokens,
        )
        logger.info(
            "本轮打分待打分=%d 成功=%d 失败=%d token(prompt=%d completion=%d total=%d)",
            result.pending,
            result.items_scored,
            result.items_failed,
            result.prompt_tokens,
            result.completion_tokens,
            result.total_tokens,
        )
        return result
    finally:
        if owned_client:
            await active.aclose()

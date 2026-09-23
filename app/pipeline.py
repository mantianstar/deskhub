"""管道编排：`fetch → score`（plan §3.1）。

日报是查询时实时计算的（不落库），所以一轮管道的全部产出就是
`items` / `item_scores` / `fetch_runs` 三张表的更新。

不依赖 FastAPI、不感知请求上下文：调度器（M6）与 CLI（`cli pipeline`）复用同一段逻辑。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app import fetcher, scorer
from app.config import Config
from app.fetcher import FetchOutcome
from app.models import FetchStatus
from app.scorer import ScoreRunResult

logger = logging.getLogger(__name__)

# 抓取与打分之间的短间隔：纯为日志可读，无功能意义（plan §11.2）
BETWEEN_STEPS_SECONDS = 1


def count_failed(outcomes: tuple[FetchOutcome, ...]) -> int:
    """本轮真正失败的源数（`ok` / `empty` 都算成功，plan §7.3）。"""
    return sum(
        1 for outcome in outcomes if outcome.status not in (FetchStatus.OK, FetchStatus.EMPTY)
    )


@dataclass(frozen=True)
class PipelineResult:
    """一轮管道的汇总，供 CLI / 调度器打印。"""

    fetch: tuple[FetchOutcome, ...]
    score: ScoreRunResult

    @property
    def items_new(self) -> int:
        return sum(outcome.items_new for outcome in self.fetch)

    @property
    def fetch_failed(self) -> int:
        return count_failed(self.fetch)


async def run(config: Config, *, score_limit: int = scorer.DEFAULT_LIMIT) -> PipelineResult:
    """跑一轮：抓取 → 等 1 秒 → 打分。

    抓取单源失败不影响整体（fetcher 内部已隔离），打分失败的条目也不写库，
    所以这里不做任何补偿：**本轮能拿到什么就是什么**。
    """
    logger.info("管道开始：fetch → score")

    fetch_outcomes = await fetcher.run_once(config)
    logger.info(
        "抓取阶段结束：%d 个源，新增 %d 条，失败 %d 个",
        len(fetch_outcomes),
        sum(outcome.items_new for outcome in fetch_outcomes),
        count_failed(tuple(fetch_outcomes)),
    )

    await asyncio.sleep(BETWEEN_STEPS_SECONDS)

    score_result = await scorer.run_pending(config, limit=score_limit)

    logger.info(
        "管道结束：新增=%d 待打分=%d 打分成功=%d 打分失败=%d token(total=%d)",
        sum(outcome.items_new for outcome in fetch_outcomes),
        score_result.pending,
        score_result.items_scored,
        score_result.items_failed,
        score_result.total_tokens,
    )
    return PipelineResult(fetch=tuple(fetch_outcomes), score=score_result)

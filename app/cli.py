"""手工命令入口：`python -m app.cli <cmd>`（plan §11.3）。

与 Web 服务共用同一套 config / db / fetcher / scorer / pipeline，方便不启动服务就调试管道。
M1 提供 `fetch` 与 `sources`，M2 补 `score`，M3 补 `pipeline`；其余命令随里程碑补齐。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import unicodedata
from collections.abc import Sequence

from app import config, db, fetcher, llm, logging_setup, pipeline, repository, scorer
from app.fetcher import FetchOutcome

PREVIEW_LIMIT = 5


# ---------------------------------------------------------------- 输出工具


def _display_width(text: str) -> int:
    """中文字符占两格，用错了表格会歪。"""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _print_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [
        max(_display_width(header[index]), *(_display_width(row[index]) for row in rows))
        for index in range(len(header))
    ]
    print("  ".join(_pad(cell, widths[index]) for index, cell in enumerate(header)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(_pad(cell, widths[index]) for index, cell in enumerate(row)))


def _print_fetch_outcomes(outcomes: Sequence[FetchOutcome]) -> None:
    """`fetch` 与 `pipeline` 共用的抓取结果表。"""
    rows = [
        (
            outcome.status,
            str(outcome.http_status or "-"),
            str(outcome.parsed_count),
            str(outcome.items_new),
            outcome.source_name,
            outcome.error or "",
        )
        for outcome in outcomes
    ]
    _print_table(("status", "http", "解析", "新增", "源", "错误"), rows)
    print(
        f"\n共 {len(outcomes)} 个源，新增 {sum(o.items_new for o in outcomes)} 条，"
        f"失败 {pipeline.count_failed(tuple(outcomes))} 个"
    )


# ---------------------------------------------------------------- 启动


def _bootstrap() -> config.Config:
    """CLI 复用服务启动顺序：日志 → 配置 → 建库 → 同步源清单。"""
    logging.basicConfig(
        level=logging.INFO,
        format=logging_setup.LOG_FORMAT,
        datefmt=logging_setup.DATE_FORMAT,
    )
    cfg = config.load()
    logging_setup.setup(cfg.app.log_path)
    db.init_db(cfg.app.db_path)
    repository.sync_sources_from_config(cfg.sources)
    return cfg


# ---------------------------------------------------------------- 命令


def cmd_fetch(args: argparse.Namespace) -> int:
    """抓取一轮；`--dry-run` 只打印解析结果，不写 items / fetch_runs / 源状态。"""
    cfg = _bootstrap()
    outcomes = asyncio.run(
        fetcher.run_once(cfg, source_id=args.source_id, dry_run=args.dry_run)
    )
    if not outcomes:
        print("没有可抓取的源（检查 config/sources.yaml 的 enabled 与 --source-id）")
        return 0

    if args.dry_run:
        print("DRY-RUN：只解析不写库")
        for outcome in outcomes:
            head = (
                f"{outcome.source_name}  status={outcome.status}  "
                f"http={outcome.http_status}  解析 {outcome.parsed_count} 条"
            )
            print(f"\n{head}  {outcome.error}" if outcome.error else f"\n{head}")
            for index, entry in enumerate(outcome.preview[:PREVIEW_LIMIT], start=1):
                print(f"  {index}. [{entry.published_at or '未知时间'}] {entry.title}")
                print(f"     {entry.url}")
            if len(outcome.preview) > PREVIEW_LIMIT:
                print(f"  …（其余 {len(outcome.preview) - PREVIEW_LIMIT} 条略）")
        return 0

    _print_fetch_outcomes(outcomes)
    return 0


def _print_score_result(cfg: config.Config, result: scorer.ScoreRunResult) -> None:
    """`score` 与 `pipeline` 共用的打分汇总。"""
    print(
        f"待打分 {result.pending} 条：成功 {result.items_scored} 条，失败 {result.items_failed} 条"
        f"（留在池中下轮重试）\n"
        f"累计 token：prompt={result.prompt_tokens} completion={result.completion_tokens} "
        f"total={result.total_tokens}；model={cfg.llm.model} prompt_version={cfg.scoring.prompt_version}"
    )
    print(f"item_scores 现有 {repository.count_scored_items()} 行")


def cmd_pipeline(args: argparse.Namespace) -> int:
    """抓取 + 打分：一次跑完整个管道（手工出报用）。"""
    cfg = _bootstrap()
    result = asyncio.run(pipeline.run(cfg, score_limit=args.limit))
    _print_fetch_outcomes(result.fetch)
    print()
    _print_score_result(cfg, result.score)
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """给待打分池里的条目打分（`--rescore` 先删旧打分再重跑）。"""
    cfg = _bootstrap()
    if args.prompt_version and not args.rescore:
        print("错误：--prompt-version 只与 --rescore 一起使用", file=sys.stderr)
        return 1

    if args.rescore:
        version = args.prompt_version or cfg.scoring.prompt_version
        deleted = repository.delete_scores_by_prompt_version(version)
        print(f"重打分：已删除 prompt_version={version} 的 {deleted} 条旧打分")

    result = asyncio.run(scorer.run_pending(cfg, limit=args.limit))
    _print_score_result(cfg, result)
    return 0


def cmd_sources(_: argparse.Namespace) -> int:
    """打印源状态表格（module / 启停 / 最近成功 / 失败次数）。"""
    _bootstrap()
    sources = repository.list_sources()
    if not sources:
        print("sources 表为空，检查 config/sources.yaml")
        return 0
    rows = [
        (
            source.module,
            source.name,
            "开" if source.enabled else "关",
            str(source.fail_count),
            source.last_ok_at or "从未成功",
            source.url,
        )
        for source in sources
    ]
    _print_table(("module", "name", "启停", "连败", "last_ok_at", "url"), rows)
    return 0


# ---------------------------------------------------------------- 入口


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="deskhub 手工命令")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="抓取 RSS 并入库")
    fetch.add_argument("--source-id", type=int, default=None, help="只抓指定源（忽略启停）")
    fetch.add_argument("--dry-run", action="store_true", help="只打印解析结果，不写库")
    fetch.set_defaults(func=cmd_fetch)

    pipeline_parser = subparsers.add_parser("pipeline", help="抓取 + 打分（手工出报）")
    pipeline_parser.add_argument(
        "--limit", type=int, default=scorer.DEFAULT_LIMIT, help="本轮最多打分条数"
    )
    pipeline_parser.set_defaults(func=cmd_pipeline)

    sources = subparsers.add_parser("sources", help="打印源状态表格")
    sources.set_defaults(func=cmd_sources)

    score = subparsers.add_parser("score", help="给待打分条目调用 LLM 打分")
    score.add_argument("--limit", type=int, default=scorer.DEFAULT_LIMIT, help="本轮最多打分条数")
    score.add_argument("--rescore", action="store_true", help="先删该 prompt_version 的旧打分再重跑")
    score.add_argument(
        "--prompt-version",
        default=None,
        help="只与 --rescore 一起用，指定要删除的旧打分版本（默认取 config.scoring.prompt_version）",
    )
    score.set_defaults(func=cmd_score)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (config.ConfigError, llm.LLMError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

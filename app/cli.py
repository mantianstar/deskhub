"""手工命令入口：`python -m app.cli <cmd>`（plan §11.3）。

与 Web 服务共用同一套 config / db / fetcher / scorer / pipeline，方便不启动服务就调试管道。
M1 提供 `fetch` 与 `sources`，M2 补 `score`，M3 补 `pipeline`，M6 补 `purge`。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from app import config, db, fetcher, llm, logging_setup, models, pipeline, repository, scorer
from app.coins import pipeline as coins_pipeline
from app.coins import profile as coins_profile
from app.coins.mxiqi import MxiqiClient, MxiqiError
from app.fetcher import FetchOutcome
from app.models import CoinFetchStatus

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


def cmd_purge(args: argparse.Namespace) -> int:
    """清理旧条目：只删 `--before` 当天 00:00（业务时区）之前抓到的。

    全量保留是默认策略，这个命令只手工触发（spec §11）；不带 `--yes` 时只预览，
    避免「想看看删多少条」变成真的删掉。
    """
    cfg = _bootstrap()
    try:
        day = date.fromisoformat(args.before)
    except ValueError:
        print(f"错误：--before 需要 YYYY-MM-DD，当前 {args.before!r}", file=sys.stderr)
        return 1
    # 按业务时区切日：否则「删 09-22 之前」会按 UTC 切，实际删到本地 09-22 08:00 前
    cutoff = repository.day_bounds_utc(cfg.app.tz, day)[0]
    scope = f"fetched_at < {cutoff}（{args.before} 00:00 {cfg.app.timezone} 之前）"

    if not args.yes:
        print(f"将删除 {repository.count_items_before(cutoff)} 条条目：{scope}")
        print("item_scores 随外键级联删除；sources / fetch_runs 保留。加 --yes 才真删")
        return 0

    deleted = repository.purge_items_before(cutoff)
    print(f"已删除 {deleted} 条：{scope}")
    return 0


# ---------------------------------------------------------------- 铜币拍卖


def cmd_coins_parse_orders(args: argparse.Namespace) -> int:
    """解析 `orders.xlsx` → 画像标签（spec §14.2）。"""
    cfg = _bootstrap()
    result = asyncio.run(coins_profile.run(cfg))
    print(
        f"读入 {result.orders_read} 条订单：解析成功 {result.orders_parsed} 条，"
        f"失败 {result.orders_failed} 条\n"
        f"写入画像标签 {result.tags_written} 个；"
        f"token(prompt={result.prompt_tokens} completion={result.completion_tokens} "
        f"total={result.total_tokens})"
    )
    print(f"coin_orders 现有 {repository.count_coin_orders()} 行")
    return 0


def cmd_coins_rebuild_profile(_: argparse.Namespace) -> int:
    """不重新调 LLM，只用已入库的 `coin_orders` 重算画像标签。"""
    _bootstrap()
    written = coins_profile.rebuild_profile()
    print(f"已用 coin_orders（{repository.count_coin_orders()} 行）重算画像标签 {written} 个")
    return 0


def cmd_coins_tags(_: argparse.Namespace) -> int:
    """打印画像标签表格（按维度分组）。"""
    _bootstrap()
    tags = repository.list_coin_profile_tags()
    if not tags:
        print("画像为空，先跑 `python -m app.cli coins-parse-orders`")
        return 0
    labels = models.COIN_DIMENSION_LABELS
    rows = [
        (
            labels.get(tag.dimension, tag.dimension),
            tag.value,
            f"{tag.weight:g}",
            str(tag.hit_count),
            "开" if tag.enabled else "关",
        )
        for tag in tags
    ]
    _print_table(("维度", "值", "权重", "次数", "启停"), rows)
    print(f"\n共 {len(tags)} 个标签，覆盖 {repository.count_coin_orders()} 条订单")
    return 0


def _coins_config(cfg: config.Config, *, headful: bool, limit: int | None) -> config.Config:
    """按 CLI 参数覆盖铜币配置（Config 是 frozen，用 replace 造副本）。"""
    coins = cfg.coins
    if headful:
        coins = replace(coins, headless=False)
    if limit is not None:
        coins = replace(coins, max_sessions_per_run=limit)
    return replace(cfg, coins=coins) if coins is not cfg.coins else cfg


def _print_coins_result(result: coins_pipeline.CoinsRunResult) -> None:
    print(
        f"状态：{result.status}\n"
        f"专场：看到 {result.sessions_seen}，入库 {result.sessions_stored}\n"
        f"拍品：入库 {result.lots_stored}，命中推荐 {result.lots_recommended}\n"
        f"查价：成功 {result.lots_priced}，当日跳过 {result.lots_pricing_skipped}，"
        f"本轮上限推迟 {result.lots_pricing_deferred}，"
        f"关键词太泛不报价 {result.lots_pricing_too_generic}，错误 {result.errors}"
    )
    if result.status == CoinFetchStatus.BLOCKED:
        print(
            "\n被麦稀奇反爬拦下：请跑 `python -m app.cli coins-fetch --headful`，"
            "在弹出的浏览器里人工过一次滑块，会话会存到 data/mxiqi-profile/ 供后续复用。",
            file=sys.stderr,
        )


def cmd_coins_fetch(args: argparse.Namespace) -> int:
    """只采集专场 + 拍品（不查价）。`--headful` / `--cdp` 用来人工过一次滑块。"""
    cfg = _coins_config(_bootstrap(), headful=args.headful, limit=args.limit)
    result = asyncio.run(coins_pipeline.run(cfg, collect_only=True, cdp_url=args.cdp))
    _print_coins_result(result)
    return 0 if result.status == CoinFetchStatus.OK else 1


def cmd_coins_price(args: argparse.Namespace) -> int:
    """只对已入库的推荐拍品查推荐价（不采集）。"""
    cfg = _coins_config(_bootstrap(), headful=args.headful, limit=None)
    result = asyncio.run(coins_pipeline.run(cfg, price_only=True, cdp_url=args.cdp))
    _print_coins_result(result)
    return 0 if result.status == CoinFetchStatus.OK else 1


def cmd_coins_pipeline(args: argparse.Namespace) -> int:
    """采集 → 匹配 → 定价，一整轮。"""
    cfg = _coins_config(_bootstrap(), headful=args.headful, limit=args.limit)
    result = asyncio.run(coins_pipeline.run(cfg, cdp_url=args.cdp))
    _print_coins_result(result)
    return 0 if result.status == CoinFetchStatus.OK else 1


def cmd_coins_dump(args: argparse.Namespace) -> int:
    """把渲染后的页面 HTML 存进 data/mxiqi-inbox/（站点改版时用它校正解析器）。"""
    cfg = _coins_config(_bootstrap(), headful=args.headful, limit=None)

    async def _dump() -> str:
        async with MxiqiClient(cfg, cdp_url=args.cdp) as client:
            url = f"{cfg.coins.base_url.rstrip('/')}{args.path}"
            return str(await client.dump(url, args.target))

    path = asyncio.run(_dump())
    print(f"已保存：{path}")
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

    purge = subparsers.add_parser("purge", help="删掉指定日期之前抓到的条目（不带 --yes 只预览）")
    purge.add_argument(
        "--before", required=True, metavar="YYYY-MM-DD", help="删除该日 00:00（业务时区）之前抓到的条目"
    )
    purge.add_argument("--yes", action="store_true", help="确认执行删除；不带则只打印将删除的条数")
    purge.set_defaults(func=cmd_purge)

    coins_parse = subparsers.add_parser(
        "coins-parse-orders", help="解析 data/orders.xlsx 生成铜币画像（调 LLM）"
    )
    coins_parse.set_defaults(func=cmd_coins_parse_orders)

    coins_rebuild = subparsers.add_parser(
        "coins-rebuild-profile", help="用已入库订单重算画像标签（不调 LLM）"
    )
    coins_rebuild.set_defaults(func=cmd_coins_rebuild_profile)

    coins_tags = subparsers.add_parser("coins-tags", help="打印铜币画像标签表格")
    coins_tags.set_defaults(func=cmd_coins_tags)

    def _add_browser_flags(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--headful", action="store_true", help="用有头浏览器跑（人工过一次滑块）"
        )
        parser.add_argument(
            "--cdp",
            default=None,
            metavar="URL",
            help="连本机已打开的 Chrome（如 http://127.0.0.1:9222），人工过滑块最稳",
        )

    coins_fetch = subparsers.add_parser("coins-fetch", help="采集麦稀奇专场与拍品（不查价）")
    _add_browser_flags(coins_fetch)
    coins_fetch.add_argument("--limit", type=int, default=None, help="本轮最多抓几个专场")
    coins_fetch.set_defaults(func=cmd_coins_fetch)

    coins_price = subparsers.add_parser("coins-price", help="对已入库的推荐拍品查推荐价")
    _add_browser_flags(coins_price)
    coins_price.set_defaults(func=cmd_coins_price)

    coins_pipeline = subparsers.add_parser("coins-pipeline", help="采集 → 匹配 → 定价（一整轮）")
    _add_browser_flags(coins_pipeline)
    coins_pipeline.add_argument("--limit", type=int, default=None, help="本轮最多抓几个专场")
    coins_pipeline.set_defaults(func=cmd_coins_pipeline)

    coins_dump = subparsers.add_parser("coins-dump", help="把渲染后的页面存进 data/mxiqi-inbox/")
    _add_browser_flags(coins_dump)
    coins_dump.add_argument("--path", default="/auction", help="站点路径，如 /auction")
    coins_dump.add_argument("--target", default="sessions", help="保存文件名（不含 .html）")
    coins_dump.set_defaults(func=cmd_coins_dump)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (config.ConfigError, llm.LLMError, MxiqiError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

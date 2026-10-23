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
from app.coins import numista as coins_numista
from app.coins import numista_web as coins_numista_web
from app.coins import coinmap as coins_coinmap
from app.coins import explore as coins_explore
from app.coins import pipeline as coins_pipeline
from app.coins import profile as coins_profile
from app.coins import translate_llm as coins_translate_llm
from app.coins.gapi import GapiError
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


def _coins_config(cfg: config.Config, *, limit: int | None) -> config.Config:
    """按 CLI 参数覆盖铜币配置（Config 是 frozen，用 replace 造副本）。"""
    if limit is None:
        return cfg
    return replace(cfg, coins=replace(cfg.coins, max_sessions_per_run=limit))


def _print_coins_result(result: coins_pipeline.CoinsRunResult) -> None:
    print(
        f"状态：{result.status}\n"
        f"专场：看到 {result.sessions_seen}，入库 {result.sessions_stored}\n"
        f"拍品：入库 {result.lots_stored}，命中推荐 {result.lots_recommended}\n"
        f"查价：成功 {result.lots_priced}，当日跳过 {result.lots_pricing_skipped}，"
        f"本轮上限推迟 {result.lots_pricing_deferred}，"
        f"关键词太泛不报价 {result.lots_pricing_too_generic}，错误 {result.errors}\n"
        f"终态：回采 {result.sessions_finalized} 场，成交 {result.lots_sold}，"
        f"未成交 {result.lots_unsold}\n"
        f"成交库：session_final {result.sales_session_final} 条，"
        f"sold_search {result.sales_search} 条"
    )


def cmd_coins_fetch(args: argparse.Namespace) -> int:
    """只采集专场 + 拍品（不查价）。走 gapi，不需要浏览器。"""
    cfg = _coins_config(_bootstrap(), limit=args.limit)
    result = asyncio.run(coins_pipeline.run(cfg, collect_only=True))
    _print_coins_result(result)
    return 0 if result.status == CoinFetchStatus.OK else 1


def cmd_coins_price(args: argparse.Namespace) -> int:
    """只对已入库的推荐拍品查推荐价（不采集）。"""
    cfg = _coins_config(_bootstrap(), limit=None)
    result = asyncio.run(coins_pipeline.run(cfg, price_only=True))
    _print_coins_result(result)
    return 0 if result.status == CoinFetchStatus.OK else 1


def cmd_coins_pipeline(args: argparse.Namespace) -> int:
    """采集 → 匹配 → 定价，一整轮。"""
    cfg = _coins_config(_bootstrap(), limit=args.limit)
    result = asyncio.run(coins_pipeline.run(cfg))
    _print_coins_result(result)
    return 0 if result.status == CoinFetchStatus.OK else 1


def cmd_coins_quality(_: argparse.Namespace) -> int:
    """打印热度覆盖率（按来源）与同 OSS key 的重复现状，用于定阈值。"""
    _bootstrap()
    coverage = repository.coin_lot_bid_coverage()
    rows: list[tuple[str, ...]] = []
    for row in coverage:
        lots = int(row["lots"] or 0)
        with_bid = int(row["lots_with_bid"] or 0)
        avg = row["avg_bid_num"]
        rows.append(
            (
                str(row["source"]),
                str(lots),
                str(with_bid),
                f"{with_bid / lots:.1%}" if lots else "-",
                "-" if avg is None else f"{avg:.2f}",
                str(int(row["lots_with_user"] or 0)),
            )
        )
    if rows:
        _print_table(("热度来源", "拍品数", "有bid_num", "非空率", "bid均值", "有bid_user_num"), rows)
    else:
        print("没有拍品")

    dupes = repository.coin_lot_image_duplicates(limit=10)
    print()
    if dupes:
        _print_table(
            ("oss_key", "拍品数"),
            [(str(row["oss_key"]), str(row["lot_count"])) for row in dupes],
        )
    else:
        print("没有同图重复的 OSS key")
    print(f"\n同图重复涉及拍品 {repository.coin_lot_image_duplicate_lot_count()} 条")
    return 0


def cmd_coins_map(_: argparse.Namespace) -> int:
    """重跑拍品 → Numista 币种映射，打印覆盖率与匹配方式分布。"""
    _bootstrap()
    result = coins_coinmap.remap_all()
    rows = [(method, str(count)) for method, count in sorted(result.by_method.items())]
    _print_table(("match_method", "拍品数"), rows)
    print(
        f"\n已映射系列 {result.lots_series_mapped}/{result.lots_total}，"
        f"已映射单版 {result.lots_issue_mapped}/{result.lots_total}"
    )
    return 0


# ---------------------------------------------------------------- Numista 参考库


def cmd_coins_series(args: argparse.Namespace) -> int:
    """按「国家 + 年份 + 面值」从 Numista 查系列并落库（输入是中文，自动转英文）。"""
    cfg = _bootstrap()
    series_list = asyncio.run(
        coins_numista.collect_series(
            cfg,
            country=args.country,
            year=args.year,
            denomination=args.denomination,
            composition=args.composition,
            limit=args.limit,
        )
    )
    if not series_list:
        print("没有匹配到系列（换个年份/面值，或检查国家映射）")
        return 0
    rows = [
        (
            str(s.numista_id),
            s.title or "",
            s.issuer or "",
            _year_range(s.min_year, s.max_year),
            s.denomination or "",
            s.composition or "",
            s.catalog_no or "",
        )
        for s in series_list
    ]
    _print_table(("系列号", "名称", "国家", "起止", "面值", "材质", "目录号"), rows)
    print(
        f"\n共入库 {len(series_list)} 个系列；"
        f"coin_series 现有 {repository.count_coin_series()} 行"
    )
    _print_numista_quota(cfg)
    return 0


def cmd_coins_issues(args: argparse.Namespace) -> int:
    """按系列号从 Numista 取该系列全部年份单版并落库。"""
    cfg = _bootstrap()
    series, issues = asyncio.run(
        coins_numista.collect_issues(cfg, series_id=args.series_id)
    )
    stored = repository.get_coin_series(series.numista_id)
    rows = [
        (
            str(i.numista_id),
            str(i.year or ""),
            i.mint_mark or "",
            i.catalog_no or "",
            str(i.mintage or ""),
            _flags(i.is_proof, i.is_essai),
            (i.comment or "")[:20],
        )
        for i in issues
    ]
    _print_table(("单版编号", "年份", "厂标", "目录号", "发行量", "精制/样币", "备注"), rows)
    print(f"\n系列 {series.numista_id}（{series.title}）入库单版 {len(issues)} 条")
    if stored is not None:
        print(f"该系列 coin_issues 现有 {len(repository.list_coin_issues(stored.id))} 行")
    _print_numista_quota(cfg)
    return 0


def cmd_coins_values(args: argparse.Namespace) -> int:
    """按系列号取 Numista 类型页的各品相估值 + Frequency 并落库（走网页表格）。

    页面挡着 Cloudflare，只能挂到你自己用调试端口开的 Chrome 上（见 README / plan §18.13.2）。
    """
    cfg = _bootstrap()
    info = asyncio.run(
        coins_numista_web.collect_issue_values(
            cfg, series_id=args.series_id, cdp_url=args.cdp
        )
    )
    rows = [
        (
            str(row.numista_issue_id),
            str(row.year or ""),
            _money(row.value_g),
            _money(row.value_vg),
            _money(row.value_f),
            _money(row.value_vf),
            _money(row.value_xf),
            _money(row.value_au),
            _money(row.value_unc),
            "—" if row.frequency is None else f"{row.frequency:g}%",
        )
        for row in info.rows
    ]
    currency = info.currency or "未知"
    print(f"估值币种：{currency}（页面按访客币种渲染）")
    _print_table(
        ("单版编号", "年份", "G", "VG", "F", "VF", "XF", "AU", "UNC", "Frequency"),
        rows,
    )
    print(
        f"\n系列 {args.series_id} 入库估值/频率 {len(info.rows)} 条；"
        f"coin_issue_values 现有 {repository.count_coin_issue_values()} 行"
    )
    print("这一列数据走的是网页表格（不占 Numista 接口额度）")
    return 0


def cmd_coins_images(args: argparse.Namespace) -> int:
    """按系列号把正 / 反图缓存到本地（走 CDP，需要本机开着调试端口的 Chrome）。

    Numista 图片带 `cross-origin-resource-policy: same-origin`，页面里嵌不了、裸 HTTP 又吃 403，
    所以借你自己的 Chrome 下载到 `data/coin_images/`，页面再从本地读。
    """
    cfg = _bootstrap()
    saved = asyncio.run(
        coins_numista_web.download_series_images(
            cfg, numista_id=args.series_id, cdp_url=args.cdp
        )
    )
    for side, path in saved.items():
        label = "正面" if side == "obverse" else "背面"
        print(f"{label}：{path.name}（{path.stat().st_size} 字节）")
    print(f"\n系列 {args.series_id} 已缓存 {len(saved)} 张 → {coins_numista_web.IMAGE_DIR}")
    return 0


def cmd_coins_explore(args: argparse.Namespace) -> int:
    """扫描外部铜币目录，写入探索候选池（候选状态）。"""
    cfg = _bootstrap()
    result = asyncio.run(coins_explore.scan_candidates(cfg, limit=args.limit))
    print(
        f"候选池扫描完成：目录 {result.listed} 条 · 取详情 {result.details} 个 · "
        f"入池 {result.stored} 个"
    )
    candidates = repository.list_coin_explore_candidates()
    rows = [
        (
            str(item.numista_id),
            item.seed_name or "",
            f"{item.size_diff:.2f}" if item.size_diff is not None else "",
            str(item.payload_cn.get("title") or ""),
            str(item.payload_cn.get("composition") or ""),
            str(item.payload_cn.get("size_mm") or ""),
        )
        for item in candidates
    ]
    if rows:
        _print_table(("系列号", "来源种子", "尺寸差", "名称", "材质", "尺寸"), rows)
    print(f"\n候选池现有 {len(candidates)} 条（忽略的不计）")
    _print_numista_quota(cfg)
    return 0


def cmd_coins_translate(args: argparse.Namespace) -> int:
    """用大模型重翻已入库系列的描述 / 备注 / 名称（读英文表 → 覆盖中文表）。

    不重采 Numista、不耗接口额度；每系列的长文本一次请求、其全部单版备注一次请求。
    枚举类字段（大类 / 形状 / 材质 / 币边 / 国家 / 执政者 / 面值）仍走内置词表。
    """
    cfg = _bootstrap()
    result = asyncio.run(
        coins_translate_llm.retranslate(cfg, series_id=args.series_id)
    )
    scope = f"系列 {args.series_id}" if args.series_id else "全部系列"
    print(
        f"{scope}：重翻 {result.series_total} 个系列"
        f"（描述/备注/名称 {result.text_fields} 段）、"
        f"单版 {result.issues_total} 条（备注 {result.comments} 条）"
    )
    if result.series_total == 0:
        print("本地没有可重翻的系列（先采回来再翻）")
    return 0


def _money(value: float | None) -> str:
    """估值显示：整数不带小数点，其余最多两位。"""
    if value is None:
        return "—"
    return f"{value:g}" if value == int(value) else f"{value:.2f}"


def _print_numista_quota(cfg: config.Config) -> None:
    """打印 Numista 本月额度用量（风控可见，别把免费额度刷完）。"""
    used = repository.count_api_calls_since(
        coins_numista.PROVIDER, repository.local_month_start_utc(cfg.app.tz)
    )
    print(f"Numista 本月已用 {used}/{cfg.coins.numista.monthly_limit} 次")


def _year_range(min_year: int | None, max_year: int | None) -> str:
    if min_year is None and max_year is None:
        return ""
    if min_year == max_year:
        return str(min_year)
    return f"{min_year or '?'}-{max_year or '?'}"


def _flags(is_proof: bool | None, is_essai: bool | None) -> str:
    """精制/样币只在「是」时显示；接口没给（None）或否 → 「—」。"""
    parts = []
    for label, value in (("精制", is_proof), ("样币", is_essai)):
        if value is True:
            parts.append(label)
    return "、".join(parts) if parts else "—"


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

    coins_fetch = subparsers.add_parser("coins-fetch", help="采集麦稀奇专场与拍品（不查价）")
    coins_fetch.add_argument("--limit", type=int, default=None, help="本轮最多抓几个专场")
    coins_fetch.set_defaults(func=cmd_coins_fetch)

    coins_price = subparsers.add_parser("coins-price", help="对已入库的推荐拍品查推荐价")
    coins_price.set_defaults(func=cmd_coins_price)

    coins_pipeline = subparsers.add_parser("coins-pipeline", help="采集 → 匹配 → 定价（一整轮）")
    coins_pipeline.add_argument("--limit", type=int, default=None, help="本轮最多抓几个专场")
    coins_pipeline.set_defaults(func=cmd_coins_pipeline)

    coins_map = subparsers.add_parser("coins-map", help="重跑拍品 → Numista 币种映射并打印覆盖率")
    coins_map.set_defaults(func=cmd_coins_map)

    coins_quality = subparsers.add_parser(
        "coins-quality", help="打印拍品热度覆盖率（按来源）与同图重复现状"
    )
    coins_quality.set_defaults(func=cmd_coins_quality)

    coins_series = subparsers.add_parser(
        "coins-series", help="按国家/年份/面值从 Numista 查系列并落库（中文输入自动转英文）"
    )
    coins_series.add_argument("--country", required=True, help="国家（中文，如 英国）")
    coins_series.add_argument("--year", type=int, required=True, help="年份，如 1900")
    coins_series.add_argument("--denomination", required=True, help="面值（中文，如 1便士）")
    coins_series.add_argument(
        "--composition", default="", help="材质（中文，如 青铜；留空=不限）——取详情后精确过滤"
    )
    coins_series.add_argument("--limit", type=int, default=None, help="最多取几个系列的详情")
    coins_series.set_defaults(func=cmd_coins_series)

    coins_issues = subparsers.add_parser(
        "coins-issues", help="按系列号从 Numista 取全部年份单版并落库"
    )
    coins_issues.add_argument("--series-id", type=int, required=True, help="系列号（Numista type id）")
    coins_issues.set_defaults(func=cmd_coins_issues)

    coins_values = subparsers.add_parser(
        "coins-values", help="按系列号取类型页的各品相估值 + Frequency 并落库（网页表格）"
    )
    coins_values.add_argument("--series-id", type=int, required=True, help="系列号（Numista type id）")
    coins_values.add_argument(
        "--cdp", default=None, help="CDP 地址，默认取 config 的 coins.numista.cdp_url"
    )
    coins_values.set_defaults(func=cmd_coins_values)

    coins_images = subparsers.add_parser(
        "coins-images", help="按系列号缓存正/反图到本地（走 CDP，需要开着调试端口的 Chrome）"
    )
    coins_images.add_argument("--series-id", type=int, required=True, help="系列号（Numista type id）")
    coins_images.add_argument(
        "--cdp", default=None, help="CDP 地址，默认取 config 的 coins.numista.cdp_url"
    )
    coins_images.set_defaults(func=cmd_coins_images)

    coins_explore_parser = subparsers.add_parser(
        "coins-explore", help="扫描外部铜币目录，写入探索候选池（默认候选状态）"
    )
    coins_explore_parser.add_argument(
        "--limit", type=int, default=None, help="单轮最多取几个候选的详情（默认取配置）"
    )
    coins_explore_parser.set_defaults(func=cmd_coins_explore)

    coins_translate = subparsers.add_parser(
        "coins-translate",
        help="用大模型重翻已入库系列的描述/备注/名称（不重采 Numista，需 DESKHUB_LLM_API_KEY）",
    )
    coins_translate.add_argument(
        "--series-id", type=int, default=None, help="只重翻该系列号；不传则重翻全部"
    )
    coins_translate.set_defaults(func=cmd_coins_translate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (
        config.ConfigError,
        llm.LLMError,
        GapiError,
        coins_numista.NumistaError,
        coins_numista_web.NumistaPageError,
        ValueError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

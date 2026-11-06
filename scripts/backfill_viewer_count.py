"""一次性回填历史场次的围观人数（coin_sessions.viewer_count）。

用法（仓库根目录执行）：
    .venv/bin/python scripts/backfill_viewer_count.py --dry-run      # 只列候选，不发请求
    .venv/bin/python scripts/backfill_viewer_count.py --limit 5      # 先小批量试跑
    .venv/bin/python scripts/backfill_viewer_count.py                # 补全全部

口径：
- 只挑 `viewer_count IS NULL` 且已开拍超过 6 小时的场次（与终态回采的缓冲一致）；
- 每场只发 1 次 `auction.get`，取 `SessionCard.viewer_count` / `view_count` 回写；
- 场次状态能映射时才一并写 status，映射不出（status=None）则跳过该场并打印，
  避免把未知状态写坏；
- 断点即「viewer_count 仍为 NULL」，中断后重跑会从剩余场次继续。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db, repository  # noqa: E402
from app.coins.gapi import GapiError, MxiqiGapi  # noqa: E402

GRACE_HOURS = 6


def list_candidates(limit: int | None) -> list[tuple[int, str]]:
    cutoff = repository.to_local_str(
        datetime.now(repository.BEIJING) - timedelta(hours=GRACE_HOURS)
    )
    sql = """
        SELECT id, mxiqi_id
        FROM coin_sessions
        WHERE viewer_count IS NULL
          AND mxiqi_id IS NOT NULL
          AND auction_at IS NOT NULL
          AND auction_at <= ?
        ORDER BY auction_at DESC
    """
    params: list[object] = [cutoff]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [(int(row["id"]), row["mxiqi_id"]) for row in rows]


async def run(limit: int | None, dry_run: bool) -> int:
    cfg = config.load()
    db.init_db(cfg.app.db_path)

    candidates = list_candidates(limit)
    print(f"待回填场次：{len(candidates)} 个（viewer_count 为空、已结束）")
    if dry_run or not candidates:
        for session_id, mxiqi_id in candidates[:10]:
            print(f"  {session_id}\t{mxiqi_id}")
        return 0

    interval = cfg.coins.request_interval_seconds
    written = skipped = failed = 0
    async with MxiqiGapi(cfg) as api:
        for index, (session_id, mxiqi_id) in enumerate(candidates):
            if index > 0 and interval > 0:
                await asyncio.sleep(random.uniform(interval * 0.6, interval * 1.4))
            try:
                detail = await api.session_detail(mxiqi_id)
            except GapiError as exc:
                failed += 1
                print(f"  失败 {mxiqi_id}：{exc}")
                continue

            card = detail.card
            if card.status is None:
                skipped += 1
                print(f"  跳过 {mxiqi_id}：status 映射为空（viewer={card.viewer_count}）")
                continue
            if card.viewer_count is None and card.view_count is None:
                skipped += 1
                print(f"  跳过 {mxiqi_id}：详情未返回围观数据")
                continue

            repository.set_coin_session_status(
                session_id,
                card.status,
                view_count=card.view_count,
                viewer_count=card.viewer_count,
            )
            written += 1
            if written % 20 == 0:
                print(f"  进度 {index + 1}/{len(candidates)}，已写 {written} 场")

    print(f"完成：写入 {written} 场，跳过 {skipped} 场，失败 {failed} 场")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="一次性回填历史场次围观人数")
    parser.add_argument("--limit", type=int, default=None, help="最多处理几个场次")
    parser.add_argument("--dry-run", action="store_true", help="只列候选，不发请求、不写库")
    args = parser.parse_args()
    return asyncio.run(run(args.limit, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())

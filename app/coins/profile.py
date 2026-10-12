"""铜币画像：`orders.xlsx` → 结构化标签（spec §14.2 / plan §18.4）。

分两步，两步都可单独跑：
1. **解析**（需要 LLM）：逐行读 `orders.xlsx`，把「宝贝名称」抽成
   `country / era / denomination / category / condition`，写 `coin_orders`。
2. **聚合**（纯本地）：按维度统计频次 → `coin_profile_tags`，作为每日筛选的匹配依据。

职责边界：本模块不依赖 FastAPI；SQL 一律走 `app.repository`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app import llm, repository
from app.config import Config
from app.models import CoinOrder

logger = logging.getLogger(__name__)

# 从名称里抽取的维度；值要求取自名称原文，便于 matcher 按关键词命中（plan §18.6）
EXTRACT_DIMENSIONS = ("country", "era", "denomination", "category", "condition")
# 参与聚合的维度：多一个 shop（来自订单的店铺列），不含 price_band（它按价格单独算）
AGG_DIMENSIONS = EXTRACT_DIMENSIONS + ("shop",)
# 维度 → `CoinOrder` 上的属性名
_DIM_ATTR = {
    "country": "country",
    "era": "era",
    "denomination": "denomination",
    "category": "category",
    "condition": "condition",
    "shop": "shop_name",
}

RETRY_HINT = "只输出 JSON"
MAX_ATTEMPTS = 2

CATEGORIES = ("铜币", "银元", "纪念币", "花钱", "纸币", "勋章", "硬币", "其他")

SYSTEM_PROMPT = """你是钱币收藏信息抽取助手，只输出 JSON，不要任何解释文字。

下面给你一批拍卖宝贝名称（含营销词），请逐条抽取以下字段：
- country：国家/地区，**必须是名称里原样出现的片段**（如「东加勒比」「英国」「香港」「澳门」「日本」「印度」「英属海峡」）；没有就 null
- era：年代，**必须是名称里原样出现的片段**（如「1808」「明治八年」「康德四年」「民国六十二年」「宣统」）；没有就 null
- denomination：面值，**必须是名称里原样出现的片段**（如「一分」「半便士」「1便士」「五元」「五角」「20瑞斯」「五分」「半克朗」）；没有就 null
- category：品类，只能取以下之一：铜币 / 银元 / 纪念币 / 花钱 / 纸币 / 勋章 / 硬币 / 其他
- condition：品相或包浆，**必须是名称里原样出现的片段**（如「巧克力包浆」「五彩包浆」「原光」）；没有就 null

规则：
- 「包老保真」「美品」「极美品」「后铸版」「极品」这类营销/修饰词不要当字段值。
- 抽不出就填 null，**不要猜、不要脑补名称里没有的信息**。
- 按输入顺序返回，条数必须与输入完全一致。

输出格式：{"items":[{"country":null,"era":null,"denomination":null,"category":null,"condition":null}, ...]}"""

USER_PROMPT_TEMPLATE = """宝贝名称：
{listing}"""


class ProfileError(ValueError):
    """画像解析失败（继承 ValueError，CLI 的统一错误处理能接住）。"""


@dataclass(frozen=True)
class RawOrder:
    """`orders.xlsx` 的原始一行（未抽字段）。"""

    order_no: str
    shop_name: str | None
    item_name: str
    price: float | None


@dataclass(frozen=True)
class TagAgg:
    """聚合出的一个标签。"""

    dimension: str
    value: str
    weight: float
    hit_count: int


@dataclass(frozen=True)
class ProfileRunResult:
    orders_read: int
    orders_parsed: int
    orders_failed: int
    tags_written: int
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ---------------------------------------------------------------- 读 orders.xlsx


def _clean_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "n/a", "未知", "无"):
        return None
    return text


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_orders(path: Path) -> list[RawOrder]:
    """读 `orders.xlsx`（列：订单号 / 店铺名称 / 宝贝名称 / 宝贝价格）。

    `订单号` 在源文件里是文本（inlineStr），所以不会丢精度；这里仍做字符串化兜底。
    表头行按首列是否为「订单号」识别，缺列（如店铺为空）不影响其余列。
    """
    from openpyxl import load_workbook

    if not path.exists():
        raise ProfileError(f"找不到订单文件：{path}")

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        orders: list[RawOrder] = []
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            cells = list(row) + [None, None, None, None]
            order_no, shop, name, price = cells[0], cells[1], cells[2], cells[3]
            order_no_text = _clean_str(order_no)
            if index == 0 and order_no_text in ("订单号", None):
                continue
            item_name = _clean_str(name)
            if order_no_text is None or item_name is None:
                continue
            orders.append(
                RawOrder(
                    order_no=order_no_text,
                    shop_name=_clean_str(shop),
                    item_name=item_name,
                    price=_to_float(price),
                )
            )
        return orders
    finally:
        workbook.close()


# ---------------------------------------------------------------- LLM 抽字段


class _Client(Protocol):
    async def complete_json(self, system: str, user: str) -> str: ...


def _extract_json_object(text: str) -> dict:
    """剥掉 ``` 围栏、取第一个 `{...}`。"""
    cleaned = text.replace("```json", "").replace("```", "")
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"响应里找不到 JSON 对象：{text.strip()[:120]!r}")
    payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层不是对象：{payload!r}")
    return payload


def parse_extraction(text: str, expected_count: int) -> list[dict[str, str | None]]:
    """把模型输出解析成字段列表；条数不符或结构不对都抛 `ValueError`。"""
    payload = _extract_json_object(text)
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("响应里缺少 items 数组")
    if len(items) != expected_count:
        raise ValueError(f"抽取条数不符：期望 {expected_count}，实际 {len(items)}")

    parsed: list[dict[str, str | None]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"items 元素不是对象：{item!r}")
        parsed.append({dimension: _clean_str(item.get(dimension)) for dimension in EXTRACT_DIMENSIONS})
    return parsed


def build_batch_prompt(orders: list[RawOrder]) -> tuple[str, str]:
    listing = "\n".join(f"{index}. {order.item_name}" for index, order in enumerate(orders, 1))
    return SYSTEM_PROMPT, USER_PROMPT_TEMPLATE.format(listing=listing)


async def extract_batch(client: _Client, orders: list[RawOrder]) -> list[dict[str, str | None]]:
    """抽一批；失败重试 1 次（附加「只输出 JSON」），仍失败抛 `ProfileError`。"""
    system, user = build_batch_prompt(orders)
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = user if attempt == 1 else f"{user}\n\n{RETRY_HINT}"
        try:
            return parse_extraction(await client.complete_json(system, prompt), len(orders))
        except (llm.LLMError, ValueError) as exc:
            last_error = exc
            logger.warning("画像抽取失败（第 %d/%d 次）：%s", attempt, MAX_ATTEMPTS, exc)
    raise ProfileError(f"重试 {MAX_ATTEMPTS} 次仍失败：{last_error}") from last_error


# ---------------------------------------------------------------- 聚合


def aggregate(orders: list[CoinOrder]) -> list[TagAgg]:
    """按维度统计频次。

    `hit_count` 是原始出现次数；`weight` 是**维度内归一化**频率（最高频 = 1.0），
    这样 matcher 把各维度相加时，不会被「英国」这种高频词一个词就顶穿阈值。
    """
    tags: list[TagAgg] = []
    for dimension in AGG_DIMENSIONS:
        attribute = _DIM_ATTR[dimension]
        counts = Counter(
            value for order in orders if (value := getattr(order, attribute)) is not None
        )
        if not counts:
            continue
        top = max(counts.values())
        for value, count in counts.most_common():
            tags.append(TagAgg(dimension, value, round(count / top, 3), count))
    return tags


def price_band_tags(prices: list[float | None]) -> list[TagAgg]:
    """按价格四分位切 4 段（不足则退化），`weight` = 落在该段的订单占比。

    v1 的 matcher 不用它（拍品标题里没有价格语义），仅用于画像页展示「我大致的价位区间」。
    """
    values = sorted(price for price in prices if price is not None)
    if len(values) < 2:
        return []
    q1, q2, q3 = statistics.quantiles(values, n=4)
    boundaries = sorted({int(round(q)) for q in (q1, q2, q3)})
    if not boundaries:
        return []

    bands: list[tuple[str, int]] = []
    lower: int | None = None
    for boundary in boundaries:
        if lower is None:
            label = f"{boundary}元以下"
            count = sum(1 for value in values if value <= boundary)
        else:
            label = f"{boundary}元" if boundary <= lower + 1 else f"{lower + 1}–{boundary}元"
            count = sum(1 for value in values if lower < value <= boundary)
        bands.append((label, count))
        lower = boundary

    tail = boundaries[-1]
    rest = sum(1 for value in values if value > tail)
    if rest:
        bands.append((f"{tail + 1}元以上", rest))

    total = len(values)
    return [
        TagAgg("price_band", label, round(count / total, 3), count)
        for label, count in bands
        if count > 0
    ]


def rebuild_profile() -> int:
    """从 `coin_orders` 重算画像（不调 LLM），返回写入的标签数。"""
    orders = repository.list_coin_orders()
    tags = aggregate(orders) + price_band_tags([order.price for order in orders])
    for tag in tags:
        repository.upsert_coin_tag(tag.dimension, tag.value, tag.weight, tag.hit_count)
    return len(tags)


# ---------------------------------------------------------------- 一轮画像


async def run(
    config: Config,
    *,
    path: Path | None = None,
    client: llm.LLMClient | None = None,
    batch_size: int | None = None,
) -> ProfileRunResult:
    """解析 `orders.xlsx` → 写 `coin_orders` → 重算 `coin_profile_tags`。

    分批并发抽取（并发数取 `coins.profile_llm.concurrency`）：单批抽取失败只记日志、跳过该批，
    不阻塞其余批次 —— 与知识聚合的「单源失败隔离」同精神；跳过的订单没入库，重跑会再试。
    落库在 gather 之后串行做，避免并发写库。
    """
    source = path or config.coins.orders_xlsx
    orders = await asyncio.to_thread(read_orders, source)
    if not orders:
        raise ProfileError(f"订单文件里没有可用行：{source}")

    owned_client = client is None
    active = client or llm.get_client(config)
    size = batch_size or config.coins.profile_llm.batch_size
    concurrency = config.coins.profile_llm.concurrency
    prompt_version = config.coins.profile_llm.prompt_version
    chunks = [orders[start : start + size] for start in range(0, len(orders), size)]
    logger.info(
        "画像开始：读入 %d 条订单，分 %d 批（每批 %d），并发 %d，prompt_version=%s",
        len(orders),
        len(chunks),
        size,
        concurrency,
        prompt_version,
    )

    semaphore = asyncio.Semaphore(concurrency)

    async def handle(index: int, chunk: list[RawOrder]) -> tuple[int, list[dict] | None]:
        async with semaphore:
            try:
                return index, await extract_batch(active, chunk)
            except ProfileError as exc:
                logger.warning("跳过第 %d 批（%d 条）：%s", index + 1, len(chunk), exc)
                return index, None

    parsed = 0
    failed = 0
    try:
        results = await asyncio.gather(*(handle(i, c) for i, c in enumerate(chunks)))
        for index, fields in sorted(results, key=lambda item: item[0]):
            chunk = chunks[index]
            if fields is None:
                failed += len(chunk)
                continue
            for raw, extracted in zip(chunk, fields):
                order = CoinOrder(
                    order_no=raw.order_no,
                    shop_name=raw.shop_name,
                    item_name=raw.item_name,
                    price=raw.price,
                    **extracted,
                )
                await asyncio.to_thread(repository.upsert_coin_order, order)
                parsed += 1

        tags_written = await asyncio.to_thread(rebuild_profile)
    finally:
        if owned_client:
            await active.aclose()

    logger.info(
        "画像结束：解析 %d 条（失败 %d）写入标签 %d 个，token(prompt=%d completion=%d total=%d)",
        parsed,
        failed,
        tags_written,
        active.usage.prompt_tokens,
        active.usage.completion_tokens,
        active.usage.total_tokens,
    )
    return ProfileRunResult(
        orders_read=len(orders),
        orders_parsed=parsed,
        orders_failed=failed,
        tags_written=tags_written,
        prompt_tokens=active.usage.prompt_tokens,
        completion_tokens=active.usage.completion_tokens,
    )

"""大模型翻译：描述 / 备注 / 名称（用户 2026-10-15；spec §14.13 / plan §18.13.4）。

分工（两套并存，各管一段）：

- **枚举类字段**（大类 / 形状 / 材质 / 币边 / 国家 / 执政者 / 面值 / 造币厂）→ 内置词表
  （`app/coins/translate.py`）——一次性映射、离线精确、零成本，也是采集时「按面值 / 材质过滤」的依据；
  词表没命中的国家 / 执政者 / 面值也会交大模型兜底（词表已译出时以词表为准）。
- **自由长文本**（系列名称 `title`、正/背面描述、系列备注 `comments`、单版备注 `comment`）→ **大模型翻译** ——
  词表只能做术语替换，长句会中英混排（如 `Melbourne & 悉尼 Mints - Dot below bottom 卷轴`），读者体验差。

三条约定（与用户确认）：

1. **降级**：没配 `DESKHUB_LLM_API_KEY` 或调用 / 解析失败 → **回退内置词表结果**，只记 warning，采集不中断。
2. **存量**：已入库的旧数据用 CLI `coins-translate` 重翻（读英文表 → 重翻 → 覆盖中文表，**不重采 Numista、不耗接口额度**）。
3. **可测**：解析是**纯函数**（`parse_series_texts` / `parse_issue_comments`），取数只负责拼 prompt + 调用，离线可测。

批量化：一个系列的长文本合成**一次**请求；该系列的全部单版备注也合成**一次**请求（按 id 回填），
避免「63 个单版 = 63 次调用」。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from app import llm, repository
from app.coins import translate
from app.config import Config
from app.models import CoinSeries

logger = logging.getLogger(__name__)

# 交给大模型的字段（其余字段仍走词表）
SERIES_TEXT_FIELDS = (
    "title",
    "issuer",
    "ruler",
    "denomination",
    "obverse_desc",
    "reverse_desc",
    "comments",
    "commemorated_topic",
)
ISSUE_TEXT_FIELDS = ("comment",)

_FIELD_LABELS = {
    "title": "系列名称",
    "issuer": "国家 / 发行方",
    "ruler": "执政者",
    "denomination": "面值",
    "obverse_desc": "正面描述",
    "reverse_desc": "背面描述",
    "comments": "系列备注",
    "commemorated_topic": "纪念主题",
    "comment": "单版备注",
}

_SYSTEM = (
    "你是钱币目录译者。把 Numista 硬币目录的英文说明翻成简洁、准确的简体中文。"
    "只输出 JSON，不要 markdown 代码块、不要任何解释。"
    "保留专有名词（人名 / 地名 / 造币厂）的通用译名、目录号（如 KM#790、Sp#3961）与数字；"
    "不增删信息；确实无法翻译的片段保留英文原文。"
)

# 模型偶尔会套 ```json fences —— 解析前剥掉
_FENCE = re.compile(r"^\s*```[A-Za-z]*\s*|\s*```\s*$")


class TranslationError(llm.LLMError):
    """大模型译文不可用（不是 JSON / 结构不对 / 全空）→ 调用方回退词表。"""


# ---------------------------------------------------------------- 纯函数：拼 prompt + 解析


def _series_prompt(fields: dict[str, str]) -> str:
    hints = "、".join(f"{key}={_FIELD_LABELS.get(key, key)}" for key in fields)
    return (
        f"字段含义：{hints}。\n"
        "把下列 JSON 的每个值翻成简体中文，返回**同键名**的 JSON（只改值）：\n"
        + json.dumps(fields, ensure_ascii=False)
    )


def _issues_prompt(items: Sequence[tuple[int, str]]) -> str:
    payload = [{"id": item_id, "text": text} for item_id, text in items]
    return (
        "下面是若干「单版备注」。把每条 text 翻成简体中文，"
        '返回 JSON：{"items":[{"id":<原 id>,"text":"<译文>"}]}；id 必须原样保留：\n'
        + json.dumps({"items": payload}, ensure_ascii=False)
    )


def _load_json(raw: str) -> object:
    text = _FENCE.sub("", raw or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TranslationError(f"大模型返回的不是 JSON：{(raw or '')[:200]}") from exc


def parse_series_texts(raw: str, keys: Sequence[str]) -> dict[str, str]:
    """译文 JSON → `{字段: 译文}`。

    只收下 keys 里、非空字符串的项（**部分缺失 OK**，缺的那几段由调用方保留词表结果）；
    一个都没解析出来才报错。
    """
    payload = _load_json(raw)
    if not isinstance(payload, dict):
        raise TranslationError(f"译文不是对象：{(raw or '')[:200]}")
    result = {
        key: payload[key].strip()
        for key in keys
        if isinstance(payload.get(key), str) and payload[key].strip()
    }
    if not result:
        raise TranslationError(f"译文里没有任何可用字段：{(raw or '')[:200]}")
    return result


def parse_issue_comments(raw: str) -> dict[int, str]:
    """译文 JSON → `{单版编号: 备注译文}`；一条都没有才报错。"""
    payload = _load_json(raw)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise TranslationError(f"译文里没有 items 数组：{(raw or '')[:200]}")
    result: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            result[item_id] = text.strip()
    if not result:
        raise TranslationError(f"译文里没有可用的备注：{(raw or '')[:200]}")
    return result


# ---------------------------------------------------------------- 取数（薄封装）


async def series_texts_zh(client: llm.LLMClient, series: object) -> dict[str, str]:
    """翻译一个系列的长文本，返回 `{字段: 译文}`（没有可翻字段 → 空 dict）。"""
    fields = {
        field: value
        for field in SERIES_TEXT_FIELDS
        if isinstance(value := getattr(series, field, None), str) and value.strip()
    }
    if not fields:
        return {}
    raw = await client.complete_json(_SYSTEM, _series_prompt(fields))
    return parse_series_texts(raw, list(fields))


async def issue_texts_zh(
    client: llm.LLMClient, items: Sequence[tuple[int, str]]
) -> dict[int, str]:
    """一次翻一批单版备注，返回 `{单版编号: 译文}`（都为空 → 空 dict）。"""
    pairs = [(item_id, text) for item_id, text in items if text and text.strip()]
    if not pairs:
        return {}
    raw = await client.complete_json(_SYSTEM, _issues_prompt(pairs))
    return parse_issue_comments(raw)


# ---------------------------------------------------------------- 存量重翻（CLI）


@dataclass(frozen=True)
class RetranslateResult:
    """一轮重翻的统计（CLI 打印用）。"""

    series_total: int
    text_fields: int
    issues_total: int
    comments: int


async def retranslate(
    config: Config, *, series_id: int | None = None, llm_client: llm.LLMClient | None = None
) -> RetranslateResult:
    """把已入库系列的描述 / 备注 / 名称用大模型重翻，覆盖写回中文表。

    读**英文表**做输入、写**中文表**，全程不碰 Numista（不耗接口额度）；单版行沿用原
    `fetched_at`（我们没重新采集，不伪造抓取时间）。`series_id` 为空则翻全部。
    """
    client = llm_client or llm.get_client(config)
    owned = llm_client is None
    try:
        if series_id is not None:
            one = repository.get_coin_series(series_id)
            series_list = [one] if one is not None else []
        else:
            series_list = repository.list_coin_series()

        result = RetranslateResult(0, 0, 0, 0)
        for en_series in series_list:
            result = await _retranslate_one(client, en_series, result)
        return result
    finally:
        if owned:
            await client.aclose()


async def _retranslate_one(
    client: llm.LLMClient, en_series: CoinSeries, result: RetranslateResult
) -> RetranslateResult:
    """重翻一个系列（系列行 + 其下全部单版行），返回累计统计。"""
    base_cn = translate.series_to_zh(en_series)
    texts: dict[str, str] = {}
    try:
        texts = await series_texts_zh(client, en_series)
    except llm.LLMError as exc:
        logger.warning("系列 %s 描述/备注翻译失败，保留词表译文：%s", en_series.numista_id, exc)
    # 统一剥 HTML（Numista 的 comments 是 HTML 片段，页面按自动转义渲染）
    cn_id = repository.upsert_coin_series_cn(
        translate.strip_html_texts(replace(base_cn, **texts))
    )
    series_total = result.series_total + 1
    text_fields = result.text_fields + len(texts)

    issues_total = result.issues_total
    comments = result.comments
    if en_series.id is not None:
        en_issues = repository.list_coin_issues(en_series.id)
        mapping: dict[int, str] = {}
        try:
            mapping = await issue_texts_zh(
                client, [(issue.numista_id, issue.comment or "") for issue in en_issues]
            )
        except llm.LLMError as exc:
            logger.warning("系列 %s 单版备注翻译失败，保留词表译文：%s", en_series.numista_id, exc)
        for en_issue in en_issues:
            base_issue = translate.issue_to_zh(en_issue)
            # 中文表的 series_id 指向中文系列的主键（与 numista._store_issues 一致）
            repository.upsert_coin_issue_cn(
                translate.strip_html_texts(
                    replace(
                        base_issue,
                        series_id=cn_id,
                        comment=mapping.get(en_issue.numista_id) or base_issue.comment,
                    )
                )
            )
            issues_total += 1
            if en_issue.numista_id in mapping:
                comments += 1

    return RetranslateResult(series_total, text_fields, issues_total, comments)

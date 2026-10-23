"""大模型翻译测试（用户 2026-10-15；plan §18.13.4）。

- **纯函数**：`parse_series_texts` / `parse_issue_comments`（容忍 ```json 围栏、部分缺失、结构不对报错）。
- **取数**：`series_texts_zh` / `issue_texts_zh` 用 `llm.FakeClient` 注入（不发真实网络）。
- **落库回退**：`collect_series` / `collect_issues` 带 `llm_client` → 中文表用大模型译文；调用失败 → 回退词表。
- **存量重翻**：`retranslate` 读英文表、覆盖中文表，不碰 Numista。

运行方式：`.venv/bin/python -m pytest tests/test_coins_translate.py -q`
"""

from __future__ import annotations

import pytest

from app import config as config_module
from app import db, repository
from app.coins import numista, translate, translate_llm
from app.llm import FakeClient, LLMError
from app.models import CoinIssue, CoinSeries

SEARCH_PAYLOAD = {
    "count": 1,
    "types": [
        {"id": 670, "title": "1 Penny - Victoria (3rd portrait)",
         "issuer": {"code": "royaume-uni", "name": "United Kingdom"},
         "min_year": 1895, "max_year": 1901, "category": "coin"},
    ],
}

TYPE_DETAIL = {
    "id": 670,
    "url": "https://en.numista.com/670",
    "title": "1 Penny - Victoria (3rd portrait)",
    "object_type": {"id": 1, "name": "Standard circulation coins"},
    "issuer": {"code": "royaume-uni", "name": "United Kingdom"},
    "min_year": 1895,
    "max_year": 1901,
    "ruler": [{"id": 270, "name": "Victoria"}],
    "value": {"text": "1 Penny"},
    "shape": "Round",
    "composition": {"text": "Bronze"},
    "edge": {"description": "Plain"},
    "references": [{"catalogue": {"id": 3, "code": "KM"}, "number": "790"}],
    "comments": "<p>The standard weight of these pieces was 48 to the pound.</p>",
    "mints": [{"id": 17, "name": "Royal Mint (Tower Hill)"}],
    "obverse": {"description": "Older diademed and veiled portrait left."},
    "reverse": {"description": "Seated figure of Britannia right."},
}

ISSUES = [
    {"id": 116868, "is_dated": True, "year": 1895, "comment": "normal tide, Proof"},
]


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


class _StubClient(numista.NumistaClient):
    """只替换 `_get`：与网络无关（同 test_coins_numista 的做法）。"""

    def __init__(self) -> None:
        super().__init__(config_module.load())

    async def _get(self, path, params=None):
        if path == "/types":
            return SEARCH_PAYLOAD
        if path.endswith("/issues"):
            return ISSUES
        return TYPE_DETAIL


# ---------------------------------------------------------------- 纯函数：解析


def test_glossary_unifies_india_and_anna():
    assert translate.to_zh("India - British") == "印度"
    assert translate.to_zh("¼ Anna") == "1⁄4 安娜"
    assert translate.to_zh("Quarter Anna") == "1⁄4 安娜"
    assert translate.to_zh("½ Anna") == "半 安娜"


def test_parse_series_texts_tolerates_fences_and_partial_fields():
    raw = '```json\n{"title": "1 便士 - 维多利亚", "comments": "标准重量。", "unknown": "x"}\n```'
    parsed = translate_llm.parse_series_texts(
        raw, ["title", "obverse_desc", "comments"]
    )
    # 只收下 keys 里的字段；缺的（obverse_desc）不出现 → 由调用方保留词表译文
    assert parsed == {"title": "1 便士 - 维多利亚", "comments": "标准重量。"}


def test_parse_series_texts_raises_when_nothing_usable():
    with pytest.raises(LLMError):
        translate_llm.parse_series_texts("不是 JSON", ["title"])
    with pytest.raises(LLMError):
        translate_llm.parse_series_texts('{"title": ""}', ["title"])


def test_parse_issue_comments_maps_by_id_and_skips_junk():
    raw = '{"items": [{"id": 116868, "text": "正常浪，精制"}, {"id": "x", "text": "跳过"}, "junk"]}'
    assert translate_llm.parse_issue_comments(raw) == {116868: "正常浪，精制"}


# ---------------------------------------------------------------- HTML 片段 → 纯文本


def test_strip_html_keeps_words_apart_and_decodes_entities():
    # 块级 / 换行标签换成空格（避免 "weightNext" 这种粘连）；其余标签删掉；实体解码
    assert (
        translate.strip_html("<p>The <i>standard</i> weight</p><br><br>Next &amp; more")
        == "The standard weight Next & more"
    )
    assert translate.strip_html("没有标签") == "没有标签"
    assert translate.strip_html("") == ""
    assert translate.strip_html(None) is None


@pytest.mark.asyncio
async def test_collect_series_strips_html_from_chinese_comments(temp_db):
    """Numista 的 comments 是 HTML 片段 → 中文表存纯文本（页面自动转义，留标签会显示成字面）。"""
    client = FakeClient(
        ['{"comments":"<p>标准重量为每磅 48 枚。<br><br><i>正面模具版别：</i></p>"}']
    )
    await numista.collect_series(
        config_module.load(),
        country="英国",
        year=1900,
        denomination="1便士",
        client=_StubClient(),
        llm_client=client,
    )

    cn = repository.get_coin_series(670, cn=True)
    assert cn is not None
    assert "<" not in cn.comments and ">" not in cn.comments
    assert cn.comments == "标准重量为每磅 48 枚。 正面模具版别："  # 块级标签变空格，不粘连
    # 英文表保留 Numista 原文
    en = repository.get_coin_series(670)
    assert en is not None and en.comments.startswith("<p>")


# ---------------------------------------------------------------- 取数（FakeClient）


@pytest.mark.asyncio
async def test_series_texts_zh_sends_only_present_fields():
    client = FakeClient(['{"title": "1 便士 - 维多利亚"}'])
    info = numista.series_from_type(TYPE_DETAIL)

    texts = await translate_llm.series_texts_zh(client, info)

    assert texts == {"title": "1 便士 - 维多利亚"}
    _, user_prompt = client.calls[0]
    assert "1 Penny - Victoria (3rd portrait)" in user_prompt  # 原文进了 prompt
    assert '"obverse_desc"' in user_prompt


@pytest.mark.asyncio
async def test_issue_texts_zh_returns_mapping_and_skips_empty_comments():
    client = FakeClient(['{"items": [{"id": 116868, "text": "正常浪，精制"}]}'])

    mapping = await translate_llm.issue_texts_zh(client, [(116868, "normal tide, Proof"), (7, "")])

    assert mapping == {116868: "正常浪，精制"}


@pytest.mark.asyncio
async def test_issue_texts_zh_makes_no_call_when_all_empty():
    client = FakeClient([])  # 没有预置回复：一旦被调用就会抛 AssertionError

    assert await translate_llm.issue_texts_zh(client, [(1, ""), (2, None or "")]) == {}
    assert client.calls == []


# ---------------------------------------------------------------- 落库：大模型 + 回退


@pytest.mark.asyncio
async def test_collect_series_uses_llm_for_title_desc_and_comments(temp_db):
    client = FakeClient(
        ['{"title":"1 便士 - 维多利亚（第三肖像）","obverse_desc":"左侧戴冠披纱头像。",'
         '"reverse_desc":"不列颠尼亚坐像向右。","comments":"标准重量：每磅 48 枚。"}']
    )
    await numista.collect_series(
        config_module.load(),
        country="英国",
        year=1900,
        denomination="1便士",
        client=_StubClient(),
        llm_client=client,
    )

    cn = repository.get_coin_series(670, cn=True)
    assert cn is not None
    # 描述 / 备注 / 名称 → 大模型译文
    assert cn.title == "1 便士 - 维多利亚（第三肖像）"
    assert cn.obverse_desc == "左侧戴冠披纱头像。"
    assert "每磅 48 枚" in cn.comments
    # 枚举字段仍走内置词表
    assert cn.object_type == "标准流通币"
    assert cn.composition == "青铜"
    assert cn.issuer == "英国"
    # 英文表原样（不受翻译影响）
    en = repository.get_coin_series(670)
    assert en is not None and "standard weight" in en.comments


@pytest.mark.asyncio
async def test_collect_series_falls_back_to_glossary_on_llm_error(temp_db):
    client = FakeClient([LLMError("429 限流")])
    await numista.collect_series(
        config_module.load(),
        country="英国",
        year=1900,
        denomination="1便士",
        client=_StubClient(),
        llm_client=client,
    )

    cn = repository.get_coin_series(670, cn=True)
    assert cn is not None
    # 回退内置词表：备注里的标准重量被替换、未收录片段保留英文；HTML 标签已剥掉
    assert "标准重量" in cn.comments
    assert "<p>" not in cn.comments
    assert "标准流通币" == cn.object_type


@pytest.mark.asyncio
async def test_collect_issues_uses_llm_for_comments(temp_db):
    client = FakeClient(
        ['{"title":"1 便士"}',
         '{"items":[{"id":116868,"text":"正常浪，精制"}]}']
    )
    await numista.collect_issues(
        config_module.load(), series_id=670, client=_StubClient(), llm_client=client
    )

    stored = repository.get_coin_series(670, cn=True)
    assert stored is not None
    cn_issues = repository.list_coin_issues(stored.id, cn=True)
    assert [i.comment for i in cn_issues] == ["正常浪，精制"]


@pytest.mark.asyncio
async def test_collect_without_llm_client_keeps_glossary_only(temp_db):
    """没配 Key（夹具已抹掉）→ 不发 LLM，中文表就是词表译文。"""
    await numista.collect_series(
        config_module.load(),
        country="英国",
        year=1900,
        denomination="1便士",
        client=_StubClient(),
    )

    cn = repository.get_coin_series(670, cn=True)
    assert cn is not None
    assert cn.title == "1 便士 - 维多利亚 (第三 肖像)"  # 词表译文
    assert "标准重量" in cn.comments


# ---------------------------------------------------------------- 存量重翻（CLI）


def _seed_english_rows() -> None:
    now = "2026-10-15T00:00:00Z"
    series_id = repository.upsert_coin_series(
        CoinSeries(
            id=None,
            numista_id=670,
            title="1 Penny - Victoria (3rd portrait)",
            url=None,
            object_type="Standard circulation coins",
            issuer="United Kingdom",
            min_year=1895,
            max_year=1901,
            ruler="Victoria",
            denomination="1 Penny",
            size_mm=None,
            weight_g=None,
            thickness_mm=None,
            shape="Round",
            composition="Bronze",
            edge="Plain",
            obverse_desc="Older diademed and veiled portrait left.",
            obverse_lettering=None,
            obverse_picture=None,
            reverse_desc="Seated figure of Britannia right.",
            reverse_lettering=None,
            reverse_picture=None,
            catalog_no="KM#790",
            comments="The standard weight was 48 to the pound.",
            mints=["Royal Mint (Tower Hill)"],
            fetched_at=now,
        )
    )
    repository.upsert_coin_issue(
        CoinIssue(
            id=None,
            numista_id=116868,
            series_id=series_id,
            year=1895,
            gregorian_year=1895,
            mint=None,
            mint_mark=None,
            variety=None,
            comment="normal tide, Proof",
            mintage=None,
            size_mm=None,
            weight_g=None,
            thickness_mm=None,
            edge=None,
            is_proof=True,
            is_essai=None,
            picture=None,
            catalog_no="Sp#3961",
            fetched_at=now,
        )
    )


@pytest.mark.asyncio
async def test_retranslate_reads_english_and_overwrites_chinese(temp_db):
    _seed_english_rows()
    client = FakeClient(
        ['{"title":"1 便士 - 维多利亚（第三肖像）","obverse_desc":"左侧戴冠披纱头像。",'
         '"reverse_desc":"不列颠尼亚坐像向右。","comments":"标准重量：每磅 48 枚。"}',
         '{"items":[{"id":116868,"text":"正常浪，精制"}]}']
    )

    result = await translate_llm.retranslate(config_module.load(), llm_client=client)

    assert (result.series_total, result.text_fields) == (1, 4)
    assert (result.issues_total, result.comments) == (1, 1)

    cn = repository.get_coin_series(670, cn=True)
    assert cn is not None and cn.title == "1 便士 - 维多利亚（第三肖像）"
    assert cn.composition == "青铜"  # 枚举字段仍走词表
    cn_issues = repository.list_coin_issues(cn.id, cn=True)
    assert [i.comment for i in cn_issues] == ["正常浪，精制"]
    # series_id 指向中文系列（与采集写入口径一致）
    assert cn_issues[0].series_id == cn.id
    # 不重采、不加行
    assert repository.count_coin_series() == 1
    assert repository.count_coin_series(cn=True) == 1
    assert repository.count_coin_issues(cn=True) == 1


@pytest.mark.asyncio
async def test_retranslate_requires_api_key(temp_db):
    """没 Key 时 CLI 应明确报错（而不是静默什么都不翻）。

    夹具已抹掉 `DESKHUB_LLM_API_KEY`，这里显式把配置里的 key 也清掉。
    """
    from dataclasses import replace

    cfg = config_module.load()
    cfg = replace(cfg, llm=replace(cfg.llm, api_key=""))

    with pytest.raises(LLMError, match="DESKHUB_LLM_API_KEY"):
        await translate_llm.retranslate(cfg)


@pytest.mark.asyncio
async def test_retranslate_series_id_limits_scope(temp_db):
    _seed_english_rows()
    client = FakeClient([])  # 未命中的系列不该发请求

    result = await translate_llm.retranslate(
        config_module.load(), series_id=999999, llm_client=client
    )

    assert result.series_total == 0
    assert client.calls == []

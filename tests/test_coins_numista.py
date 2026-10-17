"""Numista 铜币参考库测试（spec §14.13 / plan §18.13）。

- **翻译**：中文国家/面值 → Numista 英文；英文 → 中文（内置词表），纯函数。
- **解析**：`app/coins/numista.py` 的纯函数，喂**真实响应字段**（2026-10-14 实测：
  英国 1900 年 1 便士 = 系列 670）。
- **请求**：桩客户端替换 `_get`（验路径/参数）；另用 `httpx.MockTransport` 走真 `_get`
  验**风控**（月度/单轮上限、调用计数）。
- **落库**：跑 `collect_series` / `collect_issues`，验英文表 + 中文表 + 幂等。

运行方式：`.venv/bin/python -m pytest tests/test_coins_numista.py -q`
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from app import config as config_module
from app import db, repository
from app.coins import numista, translate
from app.models import CoinIssueValues

# ---------------------------------------------------------------- 真实响应样本（2026-10-14 实测）

# `GET /types?q=1 penny&issuer=royaume-uni&year=1900` —— 轻量系列卡
SEARCH_PAYLOAD = {
    "count": 3,
    "types": [
        {"id": 4576, "title": "½ Penny - Victoria (3rd portrait)",
         "object_type": {"id": 1, "name": "Standard circulation coins"},
         "issuer": {"code": "royaume-uni", "name": "United Kingdom"},
         "min_year": 1895, "max_year": 1901, "category": "coin"},
        {"id": 13215, "title": "1 Penny - Victoria (3rd portrait; Maundy issues)",
         "object_type": {"id": 1, "name": "Standard circulation coins"},
         "issuer": {"code": "royaume-uni", "name": "United Kingdom"},
         "min_year": 1893, "max_year": 1901, "category": "coin"},
        {"id": 670, "title": "1 Penny - Victoria (3rd portrait)",
         "object_type": {"id": 1, "name": "Standard circulation coins"},
         "issuer": {"code": "royaume-uni", "name": "United Kingdom"},
         "min_year": 1895, "max_year": 1901, "category": "coin"},
    ],
}

# `GET /types/670` —— 系列详情（全字段）
TYPE_DETAIL = {
    "id": 670,
    "url": "https://en.numista.com/670",
    "title": "1 Penny - Victoria (3rd portrait)",
    "object_type": {"id": 1, "name": "Standard circulation coins"},
    "issuer": {"code": "royaume-uni", "name": "United Kingdom"},
    "min_year": 1895,
    "max_year": 1901,
    "ruler": [{"id": 270, "name": "Victoria", "wikidata_id": "Q9439"}],
    "value": {"text": "1 Penny", "numerator": 1, "denominator": 240},
    "size": 30.8,
    "weight": 9.45,
    "thickness": 1.6,
    "shape": "Round",
    "composition": {"text": "Bronze"},
    "edge": {"description": "Plain"},
    "references": [
        {"catalogue": {"id": 3, "code": "KM"}, "number": "790"},
        {"catalogue": {"id": 13, "code": "Sp"}, "number": "3961"},
    ],
    "comments": "<p>The standard weight of these pieces was 48 to the pound avoirdupois.</p>",
    "mints": [{"id": 17, "name": "Royal Mint (Tower Hill)"}],
    "obverse": {
        "description": "Older diademed and veiled portrait ('Old Head') of Queen Victoria left.",
        "lettering": "VICTORIA·DEI·GRA·BRITT·REGINA·FID·DEF·IND·IMP·",
        "picture": "https://en.numista.com/catalogue/photos/royaume-uni/4128-original.jpg",
    },
    "reverse": {
        "description": "Seated figure of Britannia right.",
        "lettering": "ONE PENNY\r\n1898",
        "picture": "https://en.numista.com/catalogue/photos/royaume-uni/4129-original.jpg",
    },
}

# `GET /types/670/issues` —— 裸数组，字段有限（无造币厂名/重量/币边/图片）
ISSUES = [
    {"id": 116868, "is_dated": True, "year": 1895, "gregorian_year": 1895,
     "comment": "normal tide, Proof",
     "references": [{"catalogue": {"id": 13, "code": "Sp"}, "number": "3961"}]},
    {"id": 45228, "is_dated": True, "year": 1895, "gregorian_year": 1895, "mintage": 5395800,
     "comment": "normal tide; see comments",
     "references": [{"catalogue": {"id": 13, "code": "Sp"}, "number": "3961"}]},
    {"id": 116867, "is_dated": True, "year": 1895, "gregorian_year": 1895,
     "comment": "low tide, Proof",
     "references": [{"catalogue": {"id": 13, "code": "Sp"}, "number": "3961A"}]},
]


@pytest.fixture()
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


def _cfg(**numista_overrides):
    """真配置 + 请求间隔归零（真配置里是 2 秒，测试不该真等）。"""
    cfg = config_module.load()
    numista_cfg = replace(cfg.coins.numista, request_interval_seconds=0, **numista_overrides)
    return replace(cfg, coins=replace(cfg.coins, numista=numista_cfg))


# ---------------------------------------------------------------- 中文 → 英文（纯函数）


def test_resolve_query_translates_chinese_to_numista_english():
    assert numista.resolve_query("英国", "1便士") == ("royaume-uni", "1 penny")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1便士", "1 penny"),
        ("半便士", "half penny"),
        ("25分", "25 cent"),
        ("1000円", "1000 yen"),
        ("英镑", "pound"),
        ("一便士", "1 penny"),
    ],
)
def test_translate_denomination(text, expected):
    assert numista.translate_denomination(text) == expected


def test_resolve_issuer_accepts_existing_code():
    assert numista.resolve_issuer("royaume-uni") == "royaume-uni"


def test_resolve_issuer_rejects_unknown_chinese():
    with pytest.raises(numista.NumistaError):
        numista.resolve_issuer("瓦坎达")
    with pytest.raises(numista.NumistaError):
        numista.resolve_issuer("")
    with pytest.raises(numista.NumistaError):
        numista.translate_denomination("")


# ---------------------------------------------------------------- 英文 → 中文（内置词表）


def test_to_zh_replaces_enum_values_exactly():
    assert translate.to_zh("Standard circulation coins") == "标准流通币"
    assert translate.to_zh("Round") == "圆形"
    assert translate.to_zh("Bronze") == "青铜"
    assert translate.to_zh("Plain") == "光边"
    assert translate.to_zh("United Kingdom") == "英国"


def test_to_zh_replaces_terms_in_free_text_and_keeps_unknown():
    # 已收录词替换成中文；未收录片段保留英文
    assert translate.to_zh("normal tide, Proof") == "正常浪, 精制"
    assert translate.to_zh("Royal Mint (Tower Hill)") == "皇家铸币厂 (伦敦塔山)"
    assert translate.to_zh(None) is None
    # 整词边界：Mint 不该咬进 Mintmark
    assert translate.to_zh("Mintmark: H") == "厂标: H"


def test_series_and_issue_to_zh_translate_only_listed_fields():
    series = numista.series_from_type(TYPE_DETAIL)
    cn = translate.series_to_zh(series)

    assert cn.title == "1 便士 - 维多利亚 (第三 肖像)"
    assert cn.object_type == "标准流通币"
    assert cn.issuer == "英国"
    assert cn.ruler == "维多利亚"
    assert cn.denomination == "1 便士"
    assert cn.composition == "青铜"
    assert cn.mints == ["皇家铸币厂 (伦敦塔山)"]
    assert "标准重量" in cn.comments
    # 不翻译的字段原样复制
    assert cn.url == series.url
    assert cn.size_mm == series.size_mm
    assert cn.obverse_lettering == series.obverse_lettering

    issue_cn = translate.issue_to_zh(numista.issue_from_issue(ISSUES[0], series))
    assert issue_cn.comment == "正常浪, 精制"
    assert issue_cn.edge == "光边"


# ---------------------------------------------------------------- 解析（纯函数）


def test_series_from_type_maps_all_fields():
    s = numista.series_from_type(TYPE_DETAIL)

    assert s.numista_id == 670
    assert s.title == "1 Penny - Victoria (3rd portrait)"
    assert s.url == "https://en.numista.com/670"
    assert s.object_type == "Standard circulation coins"
    assert s.issuer == "United Kingdom"
    assert (s.min_year, s.max_year) == (1895, 1901)
    assert s.ruler == "Victoria"
    assert s.denomination == "1 Penny"
    assert (s.size_mm, s.weight_g, s.thickness_mm) == (30.8, 9.45, 1.6)
    assert s.shape == "Round"
    assert s.composition == "Bronze"
    assert s.edge == "Plain"
    assert s.mints == ["Royal Mint (Tower Hill)"]
    assert "standard weight" in s.comments
    assert s.obverse_picture.endswith("4128-original.jpg")
    assert s.reverse_picture.endswith("4129-original.jpg")
    assert s.catalog_no == "KM#790,Sp#3961"  # 全部目录号，逗号分隔


def test_catalog_no_keeps_every_reference_code():
    data = {
        "id": 1,
        "references": [
            {"catalogue": {"code": "KM"}, "number": "1"},
            {"catalogue": {"code": "Y"}, "number": "9"},
            {"catalogue": {"code": "KM"}, "number": "2"},
            {"catalogue": {"code": "KM"}},  # 无编号 → 跳过
        ],
    }
    assert numista.series_from_type(data).catalog_no == "KM#1,Y#9,KM#2"


def test_issue_inherits_series_physical_specs_and_flags_proof():
    series = numista.series_from_type(TYPE_DETAIL)

    issue = numista.issue_from_issue(ISSUES[0], series)

    assert issue.numista_id == 116868
    assert issue.series_numista_id == 670
    assert issue.year == 1895
    assert issue.gregorian_year == 1895
    assert issue.comment == "normal tide, Proof"
    assert issue.catalog_no == "Sp#3961"
    assert issue.is_proof is True
    assert issue.is_essai is False
    assert (issue.size_mm, issue.weight_g, issue.thickness_mm, issue.edge) == (30.8, 9.45, 1.6, "Plain")
    assert issue.mint is None and issue.variety is None and issue.picture is None


def test_issue_gets_mintage_and_unknown_flags_when_comment_missing():
    series = numista.series_from_type(TYPE_DETAIL)

    with_mintage = numista.issue_from_issue(ISSUES[1], series)
    assert with_mintage.mintage == 5395800
    assert with_mintage.is_proof is False

    no_comment = numista.issue_from_issue({"id": 9, "year": 1900}, series)
    assert no_comment.is_proof is None and no_comment.is_essai is None


def test_issue_keeps_gregorian_year_when_different():
    """年号纪年（如明治 6 年）保留原值 year，并单独存公历 gregorian_year。"""
    series = numista.series_from_type(TYPE_DETAIL)

    issue = numista.issue_from_issue({"id": 10, "year": 6, "gregorian_year": 1873}, series)
    assert (issue.year, issue.gregorian_year) == (6, 1873)


# ---------------------------------------------------------------- 请求（桩客户端）


class _StubClient(numista.NumistaClient):
    """只替换 `_get`：验「打了哪个路径 / 参数对不对」，与网络无关。"""

    def __init__(self, *, detail=None, issues=None) -> None:
        super().__init__(config_module.load())
        self._detail = detail or TYPE_DETAIL
        self._issues = issues if issues is not None else ISSUES
        self.calls: list[tuple[str, dict]] = []

    async def _get(self, path, params=None):
        self.calls.append((path, params or {}))
        if path == "/types":
            return SEARCH_PAYLOAD
        if path.endswith("/issues"):
            return self._issues
        return self._detail


@pytest.mark.asyncio
async def test_search_type_ids_uses_q_issuer_year():
    api = _StubClient()

    ids = await api.search_type_ids(issuer="royaume-uni", year=1900, denomination="1 penny")

    assert api.calls[-1] == ("/types", {"q": "1 penny", "issuer": "royaume-uni", "year": 1900})
    assert ids == [4576, 13215, 670]


@pytest.mark.asyncio
async def test_issues_hits_dedicated_endpoint():
    api = _StubClient()
    series = numista.series_from_type(TYPE_DETAIL)

    issues = await api.issues(670, series=series)

    assert api.calls[-1] == ("/types/670/issues", {})
    assert [i.numista_id for i in issues] == [116868, 45228, 116867]


@pytest.mark.asyncio
async def test_client_requires_api_key():
    cfg = config_module.load()
    cfg = replace(cfg, coins=replace(cfg.coins, numista=replace(cfg.coins.numista, api_key="")))

    with pytest.raises(numista.NumistaError, match="DESKHUB_NUMISTA_API_KEY"):
        async with numista.NumistaClient(cfg):
            pass


# ---------------------------------------------------------------- 风控（真 _get + MockTransport）


def _mock_client(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(
        base_url="https://api.numista.com/v3", transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_risk_control_records_usage_and_blocks_at_monthly_limit(temp_db):
    cfg = _cfg(monthly_limit=1)
    api = numista.NumistaClient(cfg, client=_mock_client(SEARCH_PAYLOAD))

    await api.search_type_ids(issuer="royaume-uni", year=1900, denomination="1 penny")

    assert api.calls_this_run == 1
    since = repository.local_month_start_utc(cfg.app.tz)
    assert repository.count_api_calls_since(numista.PROVIDER, since) == 1

    with pytest.raises(numista.NumistaError, match="本月"):
        await api.search_type_ids(issuer="royaume-uni", year=1900, denomination="1 penny")


@pytest.mark.asyncio
async def test_risk_control_blocks_at_per_run_cap(temp_db):
    cfg = _cfg(monthly_limit=100, max_requests_per_run=1)
    api = numista.NumistaClient(cfg, client=_mock_client(SEARCH_PAYLOAD))

    await api.search_type_ids(issuer="royaume-uni", year=1900, denomination="1 penny")

    with pytest.raises(numista.NumistaError, match="本轮"):
        await api.search_type_ids(issuer="royaume-uni", year=1900, denomination="1 penny")


def test_local_month_start_is_first_day_of_business_month(temp_db):
    from datetime import datetime, timezone

    moment = datetime(2026, 10, 14, 7, 8, tzinfo=timezone.utc)
    assert repository.local_month_start_utc(
        config_module.load().app.tz, now=moment
    ) == "2026-09-30T16:00:00Z"  # 上海 10-01 00:00


# ---------------------------------------------------------------- 落库（桩客户端 + 真库）


@pytest.mark.asyncio
async def test_collect_series_stores_english_and_chinese_tables(temp_db):
    cfg = config_module.load()
    api = _StubClient()

    first = await numista.collect_series(cfg, country="英国", year=1900, denomination="1便士", client=api)
    await numista.collect_series(cfg, country="英国", year=1900, denomination="1便士", client=api)

    assert len(first) == 3
    assert repository.count_coin_series() == 1  # 三张卡同一份详情 → 一行
    assert repository.count_coin_series(cn=True) == 1

    stored = repository.get_coin_series(670)
    assert stored is not None
    assert stored.catalog_no == "KM#790,Sp#3961"
    assert stored.mints == ["Royal Mint (Tower Hill)"]
    assert "standard weight" in stored.comments

    stored_cn = repository.get_coin_series(670, cn=True)
    assert stored_cn is not None
    assert stored_cn.object_type == "标准流通币" and stored_cn.composition == "青铜"
    assert stored_cn.mints == ["皇家铸币厂 (伦敦塔山)"]
    assert "标准重量" in stored_cn.comments


@pytest.mark.asyncio
async def test_collect_issues_stores_both_tables_idempotently(temp_db):
    cfg = config_module.load()
    api = _StubClient()

    series, issues = await numista.collect_issues(cfg, series_id=670, client=api)
    await numista.collect_issues(cfg, series_id=670, client=api)

    assert series.numista_id == 670
    assert len(issues) == 3
    assert repository.count_coin_issues() == 3  # 重复抓不增行
    assert repository.count_coin_issues(cn=True) == 3

    stored = repository.get_coin_series(670)
    rows = repository.list_coin_issues(stored.id)
    assert [r.numista_id for r in rows] == [116868, 45228, 116867]
    assert rows[0].catalog_no == "Sp#3961"
    assert rows[0].size_mm == 30.8 and rows[0].edge == "Plain"
    assert rows[0].is_proof is True and rows[1].is_essai is False

    stored_cn = repository.get_coin_series(670, cn=True)
    rows_cn = repository.list_coin_issues(stored_cn.id, cn=True)
    assert len(rows_cn) == 3
    assert rows_cn[0].comment == "正常浪, 精制"
    assert rows_cn[0].edge == "光边"


# ---------------------------------------------------------------- 采集精确过滤（2026-10-15）
# Numista 的 q 是模糊搜索（查 1 penny 会带出 ½ Penny），且 /types 没有面值 / 材质筛选参数
# （实测 denomination/value/currency 被忽略）→ 取详情后按 value.text / composition.text 再挡一道。


def _detail(numista_id: int, value_text: str, composition: str) -> dict:
    return {
        **TYPE_DETAIL,
        "id": numista_id,
        "value": {"text": value_text},
        "composition": {"text": composition},
    }


# 一份「模糊命中」的真实形状：半便士 + 1便士（青铜） + 1便士（银）
_FILTERABLE = {
    4576: _detail(4576, "½ Penny", "Bronze"),
    670: _detail(670, "1 Penny", "Bronze"),
    13222: _detail(13222, "1 Penny", "Silver (.925)"),
}


def _detail_stub(details: dict[int, dict]) -> numista.NumistaClient:
    """桩客户端：`/types` 返回这批 id，`/types/{id}` 按 id 返回各自详情。"""

    class _Stub(numista.NumistaClient):
        async def _get(self, path, params=None):
            if path == "/types":
                return {"count": len(details), "types": [{"id": i} for i in details]}
            return details[int(path.rsplit("/", 1)[1])]

    return _Stub(config_module.load())


@pytest.mark.asyncio
async def test_collect_series_drops_wrong_denomination(temp_db):
    """查「1便士」→ ½ Penny 不落库（用户报的问题：多余的系列没有用）。"""
    stored = await numista.collect_series(
        config_module.load(),
        country="英国",
        year=1900,
        denomination="1便士",
        client=_detail_stub(dict(_FILTERABLE)),
    )

    assert [s.numista_id for s in stored] == [670, 13222]
    assert repository.get_coin_series(4576) is None  # ½ Penny 被挡掉
    assert repository.get_coin_series(670) is not None
    assert repository.get_coin_series(13222) is not None


@pytest.mark.asyncio
async def test_collect_series_drops_wrong_composition(temp_db):
    """加材质「青铜」→ 银（.925）的那枚也被挡掉。"""
    stored = await numista.collect_series(
        config_module.load(),
        country="英国",
        year=1900,
        denomination="1便士",
        composition="青铜",
        client=_detail_stub(dict(_FILTERABLE)),
    )

    assert [s.numista_id for s in stored] == [670]
    assert repository.get_coin_series(13222) is None


@pytest.mark.asyncio
async def test_collect_series_keeps_half_penny_when_asked(temp_db):
    """查「半便士」（英文关键词 half penny）要能命中 Numista 的「½ Penny」标题（分数符号）。"""
    stored = await numista.collect_series(
        config_module.load(),
        country="英国",
        year=1900,
        denomination="半便士",
        client=_detail_stub(dict(_FILTERABLE)),
    )

    assert [s.numista_id for s in stored] == [4576]


def test_textmatch_normalizes_and_guards_digits():
    from app import textmatch

    assert textmatch.contains("1 便士", "1便士")  # 去空白
    assert not textmatch.contains("11 便士", "1便士")  # 数字边界（同 spec §14.11）
    assert textmatch.contains("½ Penny", "half penny")  # 分数符号 ↔ 英文词
    assert not textmatch.contains("½ Penny", "1 penny")
    assert textmatch.contains("任意", "")  # 空关键词 = 不限


# ---------------------------------------------------------------- 删除（清理多余系列）


@pytest.mark.asyncio
async def test_delete_coin_series_removes_series_issues_and_values(temp_db):
    cfg = config_module.load()
    api = _StubClient()
    await numista.collect_series(cfg, country="英国", year=1900, denomination="1便士", client=api)
    await numista.collect_issues(cfg, series_id=670, client=api)
    repository.upsert_coin_issue_values(
        CoinIssueValues(
            id=None,
            numista_id=116868,
            series_numista_id=670,
            currency="CNY",
            value_g=1.0,
            value_vg=None,
            value_f=None,
            value_vf=None,
            value_xf=None,
            value_au=None,
            value_unc=None,
            frequency=None,
            fetched_at=repository.utcnow(),
        )
    )
    assert repository.count_coin_series() == 1 and repository.count_coin_issues() == 3
    assert repository.list_coin_issue_values(670)

    assert repository.delete_coin_series(670) is True
    assert repository.count_coin_series() == 0
    assert repository.count_coin_series(cn=True) == 0
    assert repository.count_coin_issues() == 0  # 外键级联
    assert repository.count_coin_issues(cn=True) == 0
    assert repository.list_coin_issue_values(670) == []

    assert repository.delete_coin_series(670) is False  # 再删不命中

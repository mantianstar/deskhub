"""铜币画像测试：`orders.xlsx` 解析、LLM 抽字段、聚合、标签编辑（M7，plan §18.11）。

运行方式：`.venv/bin/python -m pytest tests/test_coins_profile.py -q`
全程不发真实网络请求、不调真实 LLM（FakeClient）。
"""

from __future__ import annotations

import pytest
from openpyxl import Workbook

from app import db, repository
from app.coins import profile
from app.llm import FakeClient
from app.models import CoinOrder


@pytest.fixture()
def temp_db(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _write_xlsx(path) -> None:
    """造一份小 orders.xlsx：表头 + 3 条正常 + 1 条缺名（应被跳过）。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["订单号", "店铺名称", "宝贝名称", "宝贝价格"])
    sheet.append(["20260927194802784508", "沼泽如烟", "Lot.7 东加勒比1959年一分", 17])
    sheet.append(["20260919030802761301", "沼泽如烟", "Lot.90 康德四年一分", 23])
    sheet.append(["20260905030702724797", "文馨钱币收藏", "Lot.230 1938年英国1便士大铜币", "1"])
    sheet.append(["20260905030702724797", "文馨钱币收藏", None, 5])  # 缺名称 → 跳过
    workbook.save(path)


# ---------------------------------------------------------------- 解析 xlsx


def test_read_orders_parses_rows_and_skips_header(temp_db, tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_xlsx(path)

    orders = profile.read_orders(path)

    assert [o.item_name for o in orders] == [
        "Lot.7 东加勒比1959年一分",
        "Lot.90 康德四年一分",
        "Lot.230 1938年英国1便士大铜币",
    ]
    assert orders[0].order_no == "20260927194802784508"  # 订单号是文本，不能丢精度
    assert orders[0].shop_name == "沼泽如烟"
    assert orders[0].price == 17.0
    assert orders[2].price == 1.0  # 字符串价格也能转


def test_read_orders_missing_file_raises(temp_db, tmp_path):
    with pytest.raises(profile.ProfileError):
        profile.read_orders(tmp_path / "nope.xlsx")


# ---------------------------------------------------------------- LLM 抽字段

GOOD_JSON = (
    '{"items":['
    '{"country":"东加勒比","era":"1959","denomination":"一分","category":"硬币","condition":null},'
    '{"country":"康德","era":"康德四年","denomination":"一分","category":"硬币","condition":null}'
    "]}"
)

# 与 `_write_xlsx` 的 3 条有效订单一一对应
GOOD_JSON_3 = (
    '{"items":['
    '{"country":"东加勒比","era":"1959","denomination":"一分","category":"硬币","condition":null},'
    '{"country":"康德","era":"康德四年","denomination":"一分","category":"硬币","condition":null},'
    '{"country":"英国","era":"1938","denomination":"1便士","category":"铜币","condition":null}'
    "]}"
)


def _raw(name: str) -> profile.RawOrder:
    return profile.RawOrder(order_no="1", shop_name="店", item_name=name, price=1.0)


def test_parse_extraction_ok():
    parsed = profile.parse_extraction(GOOD_JSON, 2)
    assert parsed[0]["country"] == "东加勒比"
    assert parsed[0]["era"] == "1959"
    assert parsed[0]["condition"] is None


def test_parse_extraction_rejects_wrong_count():
    with pytest.raises(ValueError):
        profile.parse_extraction(GOOD_JSON, 3)


def test_parse_extraction_rejects_non_json():
    with pytest.raises(ValueError):
        profile.parse_extraction("模型没给 JSON", 2)


@pytest.mark.asyncio
async def test_extract_batch_retries_with_json_hint(tmp_path):
    client = FakeClient(["不是 JSON", GOOD_JSON])
    fields = await profile.extract_batch(client, [_raw("东加勒比1959年一分"), _raw("康德四年一分")])

    assert fields[1]["era"] == "康德四年"
    assert len(client.calls) == 2
    assert client.calls[1][1].endswith(profile.RETRY_HINT)


# ---------------------------------------------------------------- 聚合


def test_aggregate_normalizes_within_dimension():
    orders = [
        CoinOrder("1", "甲店", "英国1919年一分", 5, "英国", "1919", "一分", "铜币", None),
        CoinOrder("2", "甲店", "英国1938年一分", 6, "英国", "1938", "一分", "铜币", None),
        CoinOrder("3", "乙店", "香港1980年五元", 8, "香港", "1980", "五元", "硬币", None),
    ]
    by_key = {(t.dimension, t.value): t for t in profile.aggregate(orders)}

    # 英国出现 2 次 = 维度内最高 → 权重 1.0；香港 1 次 → 0.5
    assert by_key[("country", "英国")].weight == 1.0
    assert by_key[("country", "英国")].hit_count == 2
    assert by_key[("country", "香港")].weight == 0.5
    # 面值维度：一分 2 次（1.0）、五元 1 次（0.5）
    assert by_key[("denomination", "一分")].weight == 1.0
    assert by_key[("denomination", "五元")].weight == 0.5
    # 店铺维度也在
    assert by_key[("shop", "甲店")].hit_count == 2


def test_price_band_tags_covers_all_orders():
    tags = profile.price_band_tags([1, 2, 3, 4, 5, 6, 7, 8])
    assert tags, "有足够样本时应产出价位段"
    assert sum(t.hit_count for t in tags) == 8  # 每个订单都被分到某一段
    assert all(t.dimension == "price_band" for t in tags)


def test_price_band_tags_empty_when_too_few():
    assert profile.price_band_tags([5]) == []


# ---------------------------------------------------------------- 落库与重算


def test_upsert_coin_order_is_idempotent(temp_db):
    order = CoinOrder("1", "店", "英国1919年一分", 5, country="英国")
    repository.upsert_coin_order(order)
    repository.upsert_coin_order(order)  # 同 (订单号, 名称) 覆盖，不新增

    assert repository.count_coin_orders() == 1
    assert repository.list_coin_orders()[0].country == "英国"


def test_rebuild_profile_from_coin_orders(temp_db):
    repository.upsert_coin_order(
        CoinOrder("1", "沼泽如烟", "东加勒比1959年一分", 17, "东加勒比", "1959", "一分", "硬币", None)
    )
    repository.upsert_coin_order(
        CoinOrder("2", "沼泽如烟", "东加勒比1958年一分", 12, "东加勒比", "1958", "一分", "硬币", None)
    )

    written = profile.rebuild_profile()

    assert written > 0
    tags = {(t.dimension, t.value): t for t in repository.list_coin_profile_tags()}
    assert tags[("country", "东加勒比")].hit_count == 2
    assert tags[("denomination", "一分")].weight == 1.0


def test_upsert_coin_tag_keeps_manual_enabled_but_updates_weight(temp_db):
    """重跑聚合：`weight`/`hit_count` 随数据更新，但人工改的 `enabled` 不被覆盖。

    与源清单同一取舍（plan §16）：聚合结果是「配置」，界面上的启停是运行期状态，
    只在下次重算前生效。
    """
    repository.upsert_coin_tag("country", "英国", 1.0, 3)
    tag = repository.list_coin_profile_tags()[0]
    repository.set_coin_tag_enabled(tag.id, False)

    repository.upsert_coin_tag("country", "英国", 0.6, 9)  # 重新聚合

    after = repository.get_coin_tag(tag.id)
    assert after is not None
    assert after.enabled is False  # 人工停用被保留
    assert after.weight == 0.6  # 权重随聚合更新
    assert after.hit_count == 9


def test_add_toggle_delete_coin_tag(temp_db):
    assert repository.add_coin_tag("condition", "原光", 1.5, "我自己加的") is True
    tag = repository.list_coin_profile_tags()[0]
    assert tag.note == "我自己加的"

    assert repository.add_coin_tag("condition", "原光") is False  # 已存在 → 不覆盖
    assert repository.set_coin_tag_enabled(tag.id, False) is True
    assert repository.get_coin_tag(tag.id).enabled is False

    assert repository.delete_coin_tag(tag.id) is True
    assert repository.list_coin_profile_tags() == []


# ---------------------------------------------------------------- 端到端（FakeClient）


@pytest.mark.asyncio
async def test_run_end_to_end_with_fake_client(temp_db, tmp_path):
    from app import config

    path = tmp_path / "orders.xlsx"
    _write_xlsx(path)
    cfg = config.load()

    client = FakeClient(
        [GOOD_JSON_3]  # 一批（配置 batch_size=40，3 条一次抽完）
    )
    result = await profile.run(cfg, path=path, client=client)

    assert result.orders_read == 3
    assert result.orders_parsed == 3
    assert result.orders_failed == 0
    assert repository.count_coin_orders() == 3
    assert repository.list_coin_profile_tags(), "应写出画像标签"

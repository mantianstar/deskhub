"""LLM 客户端测试：respx 拦住 HTTP，全程不访问真实厂商（plan §13.1 用例 6 的前置）。

重点覆盖 `OpenAICompatClient` 的 response_format 400 降级与「缓存不支持标记」（plan §8.1），
这部分逻辑在 FakeClient 里绕过，必须单独测。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import llm

BASE_URL = "https://api.test/v1"
ENDPOINT = f"{BASE_URL}/chat/completions"
GOOD_JSON = '{"score": 88, "reason": "讲了 LangGraph 按问题路由决定是否检索的具体做法，可直接复用"}'


def _client() -> llm.OpenAICompatClient:
    return llm.OpenAICompatClient(base_url=BASE_URL, api_key="test-key", model="test-model")


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


# ---------------------------------------------------------------- 请求形态


@pytest.mark.asyncio
async def test_request_shape_and_usage(respx_mock):
    """默认带 response_format=json_object、Bearer 鉴权，并把 usage 累计到客户端上。"""
    route = respx_mock.post(ENDPOINT).mock(return_value=_completion(GOOD_JSON))
    client = _client()
    try:
        content = await client.complete_json("system 内容", "user 内容")
    finally:
        await client.aclose()

    assert content == GOOD_JSON
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["response_format"] == {"type": "json_object"}
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert client.usage.prompt_tokens == 10
    assert client.usage.completion_tokens == 5
    assert client.usage.total_tokens == 15


# ---------------------------------------------------------------- 降级


@pytest.mark.asyncio
async def test_downgrades_on_400_and_caches_flag(respx_mock):
    """400 视为厂商不支持 response_format：当次去掉该字段重试，之后请求也不再带（plan §8.1）。"""
    bodies: list[dict] = []

    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format unsupported"}})
        return _completion(GOOD_JSON)

    respx_mock.post(ENDPOINT).mock(side_effect=responder)
    client = _client()
    try:
        assert await client.complete_json("s", "u") == GOOD_JSON
        assert client.json_format_supported is False
        # 第二次调用（新条目）直接不再带该字段，说明标记被缓存
        assert await client.complete_json("s", "u") == GOOD_JSON
    finally:
        await client.aclose()

    assert "response_format" in bodies[0]
    assert "response_format" not in bodies[1]
    assert "response_format" not in bodies[2]
    assert len(bodies) == 3


@pytest.mark.asyncio
async def test_400_without_json_mode_is_reported(respx_mock):
    """降级后仍然 400：抛 LLMError 并带上响应体片段，不静默吞掉。"""
    respx_mock.post(ENDPOINT).mock(
        return_value=httpx.Response(400, json={"error": {"message": "model not found"}})
    )
    client = _client()
    try:
        with pytest.raises(llm.LLMError) as excinfo:
            await client.complete_json("s", "u")
    finally:
        await client.aclose()

    assert "400" in str(excinfo.value)
    assert "model not found" in str(excinfo.value)


# ---------------------------------------------------------------- 失败形态


@pytest.mark.asyncio
async def test_network_error_becomes_llm_error(respx_mock):
    respx_mock.post(ENDPOINT).mock(side_effect=httpx.ConnectTimeout("connect timed out"))
    client = _client()
    try:
        with pytest.raises(llm.LLMError):
            await client.complete_json("s", "u")
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="upstream boom"),
        httpx.Response(200, text="<html>不是 JSON</html>"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}),
    ],
)
async def test_bad_responses_become_llm_error(respx_mock, response):
    respx_mock.post(ENDPOINT).mock(return_value=response)
    client = _client()
    try:
        with pytest.raises(llm.LLMError):
            await client.complete_json("s", "u")
    finally:
        await client.aclose()
    assert client.usage.total_tokens == 0


# ---------------------------------------------------------------- FakeClient


@pytest.mark.asyncio
async def test_fake_client_returns_queued_responses_and_usage_is_zero():
    client = llm.FakeClient([GOOD_JSON, llm.LLMError("boom")])
    assert await client.complete_json("s", "u1") == GOOD_JSON
    with pytest.raises(llm.LLMError):
        await client.complete_json("s", "u2")
    assert [prompt for _, prompt in client.calls] == ["u1", "u2"]
    assert client.usage.total_tokens == 0
    with pytest.raises(AssertionError):
        await client.complete_json("s", "u3")
    await client.aclose()

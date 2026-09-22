"""LLM 客户端抽象（plan §8.1）。

不引厂商 SDK，只走 OpenAI 兼容的 `POST {base_url}/chat/completions`，这样换厂商只改配置。

- `OpenAICompatClient`：真实调用。尽量带 `response_format={"type":"json_object"}`，
  遇到 400 视为厂商不支持，降级为纯 prompt 约束并缓存标记，后续请求不再带该字段。
- `FakeClient`：测试注入，可返回固定 JSON 或故意返回坏 JSON。

实现侧对 plan §8.1 的两处补充（M2 实测结论见 tasks.md）：
1. 协议上加了 `usage` 属性 —— 协议签名保持 `complete_json(system, user) -> str` 不变，
   token 累计挂在客户端上，供 scorer 打印成本（plan §8.4 要求可核对账单）。
2. 协议上加了 `aclose()` —— 一个 run 共用一个 httpx 连接池，由 scorer 负责关闭。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Config

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_PATH = "/chat/completions"
# chat 场景经验值：超时短于 30s 会把正常的长回复打成失败（skill 手册同样建议 30s）
LLM_TIMEOUT_SECONDS = 30
# 落在日志里的响应片段长度：够定位问题，又不至于把整页 HTML 灌进日志
ERROR_BODY_PREVIEW = 300


class LLMError(Exception):
    """调用 LLM 失败：网络、鉴权、协议不符、响应结构不是 chat completion。"""


@dataclass
class Usage:
    """客户端累计 token（一个 run 一份）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMClient(Protocol):
    async def complete_json(self, system: str, user: str) -> str:
        """返回模型输出的原始文本；解析成 JSON 是 scorer 的事。"""
        ...

    @property
    def usage(self) -> Usage: ...

    async def aclose(self) -> None: ...


# ---------------------------------------------------------------- OpenAI 兼容实现


class OpenAICompatClient:
    """OpenAI 兼容协议的 chat completions 客户端。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        timeout_seconds: int = LLM_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        # 厂商是否支持 response_format=json_object；不支持时本进程内不再重试该字段
        self.json_format_supported = True
        self.usage = Usage()
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete_json(self, system: str, user: str) -> str:
        response = await self._post(system, user, json_mode=self.json_format_supported)
        if response.status_code == 400 and self.json_format_supported:
            # 按 plan §8.1：400 即认定不支持 response_format，降级后不再带该字段
            self.json_format_supported = False
            logger.warning(
                "LLM 对 response_format=json_object 返回 400，降级为纯 prompt 约束：%s",
                _body_preview(response),
            )
            response = await self._post(system, user, json_mode=False)
        if response.status_code >= 400:
            raise LLMError(f"HTTP {response.status_code}：{_body_preview(response)}")

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"响应不是 JSON：{_body_preview(response)}") from exc
        self._accumulate_usage(payload)
        return _extract_content(payload)

    async def _post(self, system: str, user: str, *, json_mode: bool) -> httpx.Response:
        body: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        try:
            return await self._client.post(
                f"{self.base_url}{CHAT_COMPLETIONS_PATH}",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        except httpx.HTTPError as exc:
            # 网络层错误一并转成 LLMError，让 scorer 按「本条失败」处理而不是崩掉整轮
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

    def _accumulate_usage(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return
        self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)


def _extract_content(payload: object) -> str:
    """从 chat completion 响应里取文本；结构不对就报错（不吞成空字符串）。"""
    if not isinstance(payload, dict):
        raise LLMError(f"响应不是对象：{str(payload)[:ERROR_BODY_PREVIEW]}")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError(f"响应里没有 choices：{str(payload)[:ERROR_BODY_PREVIEW]}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise LLMError(f"响应里没有可用的 content：{str(payload)[:ERROR_BODY_PREVIEW]}")
    return content


def _body_preview(response: httpx.Response) -> str:
    return (response.text or "")[:ERROR_BODY_PREVIEW]


# ---------------------------------------------------------------- 测试替身


class FakeClient:
    """按顺序吐出预置回复，供测试注入（plan §8.1）。

    `responses` 的元素是字符串就返回它，是异常就抛它 —— 这样既能造坏 JSON，
    也能造「重试一次后成功」。
    """

    def __init__(self, responses: Sequence[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.usage = Usage()

    async def complete_json(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self._responses:
            raise AssertionError("FakeClient 的预置回复已用尽")
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------- 工厂


def get_client(config: Config) -> LLMClient:
    """按配置造客户端；没有 key 直接报错，避免拿空 key 去撞 401。"""
    if not config.llm.api_key:
        raise LLMError(
            f"未设置环境变量 {config.llm.api_key_env}，无法调用 LLM；"
            "把它写进 .env 或环境变量后重试"
        )
    return OpenAICompatClient(
        base_url=config.llm.base_url,
        api_key=config.llm.api_key,
        model=config.llm.model,
        temperature=config.scoring.temperature,
    )

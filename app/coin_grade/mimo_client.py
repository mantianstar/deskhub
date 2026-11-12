"""小米 mimo 多模态客户端：只服务铜币品相评级，不走 app.llm 的既有调用路径。

- OpenAI 兼容 `POST {base_url}/chat/completions`，本地图片转成 data URL 内联提交，
  http(s) 链接原样透传。
- 同步实现即可：CLI 单次评级，不需要并发。
"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

import httpx

CHAT_COMPLETIONS_PATH = "/chat/completions"
ERROR_BODY_PREVIEW = 500


class MimoError(Exception):
    """mimo 调用失败（配置、网络、响应结构、图片读取）。"""


class MimoClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.1,
        timeout_seconds: int = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self._api_key = api_key
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_seconds))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MimoClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def grade(self, *, system: str, user: str, images: list[Path | str]) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": self._user_content(user, images)},
            ],
            "temperature": self.temperature,
        }
        try:
            response = self._client.post(
                f"{self.base_url}{CHAT_COMPLETIONS_PATH}",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        except httpx.HTTPError as exc:
            raise MimoError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code >= 400:
            raise MimoError(
                f"HTTP {response.status_code}：{(response.text or '')[:ERROR_BODY_PREVIEW]}"
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise MimoError(
                f"响应不是 JSON：{(response.text or '')[:ERROR_BODY_PREVIEW]}"
            ) from exc
        return self._extract_content(payload)

    @staticmethod
    def _user_content(user: str, images: list[Path | str]) -> list[dict[str, object]]:
        if not images:
            raise MimoError("至少需要一张图片（正面）")
        parts: list[dict[str, object]] = [{"type": "text", "text": user}]
        for raw_path in images:
            if str(raw_path).startswith(("http://", "https://")):
                parts.append({"type": "image_url", "image_url": {"url": str(raw_path)}})
                continue
            path = Path(raw_path)
            if not path.is_file():
                raise MimoError(f"图片不存在：{path}")
            mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
            )
        return parts

    @staticmethod
    def _extract_content(payload: object) -> str:
        if not isinstance(payload, dict):
            raise MimoError(f"响应不是对象：{str(payload)[:ERROR_BODY_PREVIEW]}")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise MimoError(f"响应里没有 choices：{str(payload)[:ERROR_BODY_PREVIEW]}")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise MimoError(f"响应里没有可用的 content：{str(payload)[:ERROR_BODY_PREVIEW]}")
        return content

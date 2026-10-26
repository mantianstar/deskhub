#!/usr/bin/env python3
"""本地 OpenAI 兼容转发代理：把每次响应的 usage 与本次请求的动作 / 对象记进 JSONL，供 token_cost.py --usage 出表。

用法：
    export DEEPSEEK_API_KEY=sk-...
    python utils/llm_proxy/llm_proxy.py --port 8787
    # 每次请求一行，默认写 data/llm_usage.jsonl

Trae 侧：
    设置 → 模型 → + 添加模型 → 服务商 DeepSeek（或 OpenAI 兼容）
    自定义请求地址：http://127.0.0.1:8787
    API 密钥：你的 DeepSeek Key
    之后在对话里切到该模型，收尾用：
    python utils/token_cost/token_cost.py --usage data/llm_usage.jsonl

两种请求都记：
- 流式（stream=true）：自动注入 stream_options.include_usage=true，从 SSE 里取 usage 后再原样转发。
- 非流式：直接读响应 JSON 的 usage。

只监听 127.0.0.1，不对外暴露；鉴权头原样转发给上游。
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

DEFAULT_PORT = 8787
DEFAULT_UPSTREAM = "https://api.deepseek.com"
DEFAULT_LOG = "data/llm_usage.jsonl"

# 逐跳与长度相关头由代理自己重算，不原样转发
_DROP_REQUEST_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}
_DROP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding"}
_LOG_LOCK = threading.Lock()

# 工具名 → 动作；对象取第一个命中的字符串参数
_TOOL_ACTIONS = {
    "read": "读取",
    "grep": "搜索",
    "glob": "查找",
    "ls": "列目录",
    "searchreplace": "编辑",
    "edit": "编辑",
    "write": "写入",
    "runcommand": "命令",
    "checkcommandstatus": "查命令",
    "stopcommand": "停命令",
    "todowrite": "任务清单",
    "task": "子代理",
    "skill": "技能",
}
_TOOL_ARG_KEYS = ("file_path", "path", "command", "pattern", "query", "description", "name")
_OBJECT_LIMIT = 100  # 工具参数（路径 / 命令 / 搜索词）
_TOPIC_LIMIT = 30  # 用户提问只留一句短标题，不抄全文
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def extract_usage(payload: object) -> dict[str, int] | None:
    """从 chat completion 响应体里取 usage；DeepSeek 还会带缓存命中数。"""
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "prompt_cache_hit_tokens": int(usage.get("prompt_cache_hit_tokens") or 0),
        "prompt_cache_miss_tokens": int(usage.get("prompt_cache_miss_tokens") or 0),
    }


class SseUsageCollector:
    """边转发边攒 SSE 行，取带 usage 的那条（通常是 [DONE] 前最后一帧）。"""

    def __init__(self) -> None:
        self._buffer = b""
        self._usage: dict[str, int] | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._take(line)

    def _take(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            usage = extract_usage(json.loads(payload))
        except json.JSONDecodeError:
            return
        if usage:
            self._usage = usage

    def usage(self) -> dict[str, int] | None:
        self._take(self._buffer)
        return self._usage


def describe_request(payload: object) -> tuple[str, str]:
    """从请求体最后一条消息推「动作 / 对象」：工具调用按函数名映射动作，首个关键参数当对象。"""
    if not isinstance(payload, dict):
        return "", ""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return "", ""
    last = messages[-1]
    if not isinstance(last, dict):
        return "", ""
    if last.get("role") == "tool":
        return _describe_tool_call(_find_tool_call(messages, last.get("tool_call_id")))
    calls = last.get("tool_calls")
    if isinstance(calls, list) and calls:
        return _describe_tool_call(calls[0])
    text = _clip(_plain_text(last.get("content")), _TOPIC_LIMIT)
    action = {"user": "提问", "system": "系统", "assistant": "回复"}.get(str(last.get("role")), "消息")
    return action, text


def _plain_text(content: object) -> str:
    """content 可能是字符串，也可能是 Trae 的文本块列表；只取文本并去掉注入提醒。

    对象列要的是「在问什么」，不是整段请求体，故提醒块与块结构都不进日志。
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        text = ""
    return _REMINDER.sub(" ", text).strip()


def _find_tool_call(messages: list, call_id: object) -> object:
    """工具结果消息只带 tool_call_id，回查上一条 assistant 消息里的函数名与参数。"""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        for call in calls:
            if isinstance(call, dict) and call_id and call.get("id") == call_id:
                return call
        if not call_id:
            return calls[-1]
    return None


def _describe_tool_call(call: object) -> tuple[str, str]:
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        return "", ""
    name = _bare_name(str(function.get("name") or ""))
    return _TOOL_ACTIONS.get(name, name or "工具"), _clip(_tool_object(function.get("arguments")))


def _tool_object(arguments: object) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    if not isinstance(arguments, dict):
        return ""
    for key in _TOOL_ARG_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _bare_name(name: str) -> str:
    return name.replace("::", ".").split(".")[-1].lower()


def _clip(text: str, limit: int = _OBJECT_LIMIT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _prepare(body: bytes, content_type: str) -> tuple[bytes, bool, str, str, str]:
    """返回 (转发体, 是否流式, 模型名, 动作, 对象)；流式时补上 include_usage，否则拿不到 usage。"""
    if "json" not in (content_type or "") or not body:
        return body, False, "", "", ""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body, False, "", "", ""
    if not isinstance(data, dict):
        return body, False, "", "", ""
    stream = bool(data.get("stream"))
    action, target = describe_request(data)
    if stream:
        options = data.get("stream_options")
        if not isinstance(options, dict):
            options = {}
        if not options.get("include_usage"):
            options["include_usage"] = True
            data["stream_options"] = options
    return json.dumps(data, ensure_ascii=False).encode(), stream, str(data.get("model") or ""), action, target


def _append(
    path: Path, route: str, model: str, stream: bool, usage: dict[str, int] | None, action: str, target: str
) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "path": route,
        "model": model,
        "stream": stream,
        "action": action,
        "object": target,
        **(usage or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _LOG_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = DEFAULT_UPSTREAM
    log_path = Path(DEFAULT_LOG)

    def do_GET(self) -> None:  # noqa: N802 —— 基类方法名固定
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def log_message(self, fmt: str, *args) -> None:
        return  # 不往 stderr 刷每个请求

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        body, stream, model, action, target = _prepare(body, self.headers.get("Content-Type", ""))
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS}
        headers["Content-Length"] = str(len(body))
        path = _endpoint_path(self.path)
        url = f"{self.upstream}{path}"
        try:
            with httpx.Client(timeout=None) as client:
                with client.stream(self.command, url, content=body, headers=headers) as response:
                    print(f"{self.command} {self.path} → {path} [{response.status_code}]", flush=True)
                    self._relay(response, stream, model, action, target)
        except httpx.HTTPError as exc:
            print(f"{self.command} {self.path} → {url} [ERROR {type(exc).__name__}]", flush=True)
            self._send_text(502, f"上游请求失败：{type(exc).__name__}: {exc}")

    def _relay(self, response: httpx.Response, stream: bool, model: str, action: str, target: str) -> None:
        if not stream:
            raw = response.read()
            usage = extract_usage(_loads(raw))
            if usage:  # 401/400 这类没有 usage，不写空行污染成本表
                _append(self.log_path, self.path, model, False, usage, action, target)
            self._send_head(response, len(raw))
            self.wfile.write(raw)
            self.wfile.flush()
            return

        self.send_response(response.status_code)
        for key, value in response.headers.items():
            if key.lower() not in _DROP_RESPONSE_HEADERS:
                self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        collector = SseUsageCollector()
        for chunk in response.iter_bytes():
            self.wfile.write(chunk)
            self.wfile.flush()
            collector.feed(chunk)
        usage = collector.usage()
        if usage:
            _append(self.log_path, self.path, model, True, usage, action, target)

    def _send_head(self, response: httpx.Response, length: int) -> None:
        self.send_response(response.status_code)
        for key, value in response.headers.items():
            if key.lower() not in _DROP_RESPONSE_HEADERS:
                self.send_header(key, value)
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def _send_text(self, status: int, text: str) -> None:
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()


def _loads(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _normalize_path(path: str) -> str:
    """合并重复斜杠、去掉重复的 /v1 前缀。

    Trae 填自定义地址时若带结尾斜杠会得到 `//v1/chat/completions`；有些服务商会再拼一次 `/v1`，
    两种都会被上游当 404，这里统一收成单斜杠单前缀。
    """
    while "//" in path:
        path = path.replace("//", "/")
    while path.startswith("/v1/v1"):
        path = path[3:]
    return path or "/"


def _endpoint_path(path: str) -> str:
    """Trae 把「自定义请求地址」当完整端点，只填主机时它会 POST 到 `/`；按对话补全成聊天端点。"""
    path = _normalize_path(path)
    return "/chat/completions" if path == "/" else path


def serve(port: int = DEFAULT_PORT, upstream: str = DEFAULT_UPSTREAM, log_path: str | Path = DEFAULT_LOG) -> ThreadingHTTPServer:
    handler = type(
        "BoundProxyHandler",
        (ProxyHandler,),
        {"upstream": upstream.rstrip("/"), "log_path": Path(log_path)},
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="转发 OpenAI 兼容请求并记录逐请求 usage")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM, help="上游地址，默认 DeepSeek 官方")
    parser.add_argument("--log", default=DEFAULT_LOG, help="usage 落库路径（JSONL）")
    args = parser.parse_args()

    server = serve(args.port, args.upstream, args.log)
    print(f"代理已启动：http://127.0.0.1:{server.server_address[1]} → {args.upstream}")
    print(f"usage 落库：{args.log}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()

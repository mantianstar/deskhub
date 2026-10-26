"""本地上游代理的用例：usage 提取、流式注入与落库。

代理是独立脚本（`utils/llm_proxy/llm_proxy.py`，不在 `app` 包里），按 tokenizer 的同一方式动态加载。
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
PROXY_PATH = ROOT / "utils" / "llm_proxy" / "llm_proxy.py"


def _load_proxy():
    spec = importlib.util.spec_from_file_location("llm_proxy_under_test", PROXY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_token_cost():
    cost = importlib.util.spec_from_file_location(
        "token_cost_under_test", ROOT / "utils" / "token_cost" / "token_cost.py"
    )
    module = importlib.util.module_from_spec(cost)
    cost.loader.exec_module(module)
    return module


proxy = _load_proxy()


def test_extract_usage_keeps_cache_fields():
    usage = proxy.extract_usage(
        {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                   "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 2}}
    )
    assert usage == {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 2,
    }
    assert proxy.extract_usage({"choices": []}) is None


def test_sse_collector_takes_usage_frame_across_chunks():
    collector = proxy.SseUsageCollector()
    collector.feed(b'data: {"choices":[{"delta":{"content":"hi"}}],"usage":null}\n\n')
    collector.feed(b'data: {"choices":[],"usage":{"prompt_tokens":11,')
    collector.feed(b'"completion_tokens":7}}\n\ndata: [DONE]\n\n')
    assert collector.usage()["prompt_tokens"] == 11
    assert collector.usage()["completion_tokens"] == 7


def test_normalize_path_collapses_slashes_and_v1():
    assert proxy._normalize_path("//v1/chat/completions") == "/v1/chat/completions"
    assert proxy._normalize_path("/v1/v1/chat/completions") == "/v1/chat/completions"
    assert proxy._normalize_path("/chat/completions") == "/chat/completions"


def test_endpoint_path_maps_root_to_chat_completions():
    """Trae 只填主机时会 POST 到 `/`，要补成聊天端点，否则上游 404。"""
    assert proxy._endpoint_path("/") == "/chat/completions"
    assert proxy._endpoint_path("//v1/v1/chat/completions") == "/v1/chat/completions"


def test_describe_request_maps_tool_name_and_first_arg():
    """动作取工具名，对象取首个关键参数；用户消息按提问记。"""
    command = {"messages": [{"role": "assistant", "tool_calls": [
        {"function": {"name": "RunCommand", "arguments": json.dumps({"command": "pytest tests -q"})}}]}]}
    assert proxy.describe_request(command) == ("命令", "pytest tests -q")

    user = {"messages": [{"role": "user", "content": "看下代理的\n使用情况"}]}
    assert proxy.describe_request(user) == ("提问", "看下代理的 使用情况")
    assert proxy.describe_request({}) == ("", "")


def test_describe_request_follows_tool_call_id():
    """最后一条是工具结果时，按 tool_call_id 回查函数名与参数。"""
    payload = {"messages": [
        {"role": "assistant", "tool_calls": [
            {"id": "a", "function": {"name": "Read", "arguments": json.dumps({"file_path": "/repo/A.md"})}},
            {"id": "b", "function": {"name": "SearchReplace", "arguments": json.dumps({"file_path": "/repo/B.md"})}},
        ]},
        {"role": "tool", "tool_call_id": "b", "content": "ok"},
    ]}
    assert proxy.describe_request(payload) == ("编辑", "/repo/B.md")


def test_describe_request_keeps_only_topic_from_content_blocks():
    """Trae 的 content 是文本块列表：只取用户原话压成短标题，注入的 system-reminder 不算对象。"""
    question = "把代理记的对象列压成一句短标题就行，不要再把完整内容抄进日志里了"
    payload = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>\nThe user opened the file A.md\n</system-reminder>"},
        {"type": "text", "text": question},
    ]}]}
    action, target = proxy.describe_request(payload)
    assert action == "提问"
    assert len(target) == proxy._TOPIC_LIMIT
    assert target.endswith("…") and question.startswith(target[:-1])


class _Upstream(BaseHTTPRequestHandler):
    """替身上游：流式返回两帧，非流式返回一段 JSON；同时校验 include_usage 被注入。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("model") == "unauthorized":
            self._send(401, b'{"error":"Authentication Fails"}')
            return
        if body.get("stream"):
            if not (body.get("stream_options") or {}).get("include_usage"):
                self._send(400, b'{"error":"missing include_usage"}')
                return
            payload = (
                b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":21,"completion_tokens":9,"total_tokens":30}}\n\n'
                b"data: [DONE]\n\n"
            )
            self._send(200, payload, "text/event-stream")
            return
        self._send(200, json.dumps(
            {"choices": [{"message": {"content": "{}"}}],
             "usage": {"prompt_tokens": 13, "completion_tokens": 4, "total_tokens": 17}}
        ).encode())

    def _send(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


@pytest.fixture()
def servers(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    log_path = tmp_path / "usage.jsonl"
    proxy_server = proxy.serve(0, f"http://127.0.0.1:{upstream.server_address[1]}", log_path)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (upstream, proxy_server)]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{proxy_server.server_address[1]}", log_path
    finally:
        for server in (proxy_server, upstream):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def _records(log_path: Path) -> list[dict]:
    """流式请求的落库发生在响应发完之后，给一点时间再读。"""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if log_path.exists() and log_path.read_text(encoding="utf-8").strip():
            return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        time.sleep(0.02)
    raise AssertionError(f"usage 未落库：{log_path}")


def test_proxy_logs_usage_for_non_stream(servers):
    base, log_path = servers
    response = httpx.post(f"{base}/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
    assert response.status_code == 200
    assert response.json()["usage"]["total_tokens"] == 17

    record = _records(log_path)[-1]
    assert record["model"] == "deepseek-chat"
    assert record["stream"] is False
    assert record["prompt_tokens"] == 13
    assert record["completion_tokens"] == 4


def test_proxy_logs_usage_for_stream(servers):
    base, log_path = servers
    response = httpx.post(
        f"{base}/v1/chat/completions",
        json={"model": "deepseek-chat", "messages": [], "stream": True},
    )
    assert response.status_code == 200
    assert "[DONE]" in response.text

    record = _records(log_path)[-1]
    assert record["stream"] is True
    assert record["prompt_tokens"] == 21
    assert record["completion_tokens"] == 9


def test_proxy_logs_action_and_object(servers):
    """成本清单的「动作 / 对象」两列取自请求体的最后一条消息并随 usage 落库。"""
    base, log_path = servers
    httpx.post(f"{base}/v1/chat/completions", json={
        "model": "m",
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {
                "name": "SearchReplace", "arguments": json.dumps({"file_path": "/repo/RULES.md"})}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ],
    })

    record = _records(log_path)[-1]
    assert record["action"] == "编辑"
    assert record["object"] == "/repo/RULES.md"


def test_failed_request_is_not_logged(servers):
    """没有 usage 的响应（401 等）不落库，避免成本表混进全 0 行。"""
    base, log_path = servers
    response = httpx.post(f"{base}/v1/chat/completions", json={"model": "unauthorized", "messages": []})
    assert response.status_code == 401
    assert not log_path.exists()


def test_usage_log_feeds_token_cost(servers, tmp_path):
    """落库文件直接喂给 token_cost.py 的 --usage，不再反推底座。"""
    base, log_path = servers
    httpx.post(f"{base}/v1/chat/completions", json={"model": "m", "messages": []})
    _records(log_path)  # 等落库

    module = _load_token_cost()
    loaded = module.load_usage_rows(log_path)
    assert loaded == [{"action": "", "object": "", "prompt_tokens": 13, "completion_tokens": 4}]


def test_usage_table_clips_long_object(capsys):
    """对象列只留标识：长提问截到 30 字，文件路径转相对路径且不丢文件名。"""
    module = _load_token_cost()
    module.print_usage_table([
        {"action": "提问", "object": "很长的提问" * 20, "prompt_tokens": 10, "completion_tokens": 2},
        {"action": "读取", "object": str(module.ROOT / "utils/token_cost/token_cost.py"),
         "prompt_tokens": 10, "completion_tokens": 2},
    ])
    out = capsys.readouterr().out
    assert "…" in out
    assert ("很长的提问" * 20) not in out
    assert "utils/token_cost/token_cost.py" in out

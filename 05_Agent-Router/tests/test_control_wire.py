"""Exercise the real SDK against loopback-only mock provider HTTP servers."""

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from jui.control_models import ModelEngine


@contextmanager
def provider_server(protocol, status=200, stream=False, tools=False, complete=True):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "body": body})
            if status != 200:
                value = {
                    "error": {
                        "message": "DO-NOT-EXPOSE-SECRET",
                        "type": "rate_limit_error",
                        "code": "rate_limit_exceeded",
                    }
                }
            elif protocol == "anthropic":
                value = {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": "OK"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            elif protocol == "gemini":
                value = {
                    "candidates": [
                        {
                            "index": 0,
                            "content": {"role": "model", "parts": [{"text": "OK"}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 1,
                        "candidatesTokenCount": 1,
                        "totalTokenCount": 2,
                    },
                }
            else:
                message = {"role": "assistant", "content": "OK"}
                if tools:
                    message.update(
                        content=None,
                        tool_calls=[
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read",
                                    "arguments": '{"path":"README.md"}',
                                },
                            }
                        ],
                    )
                value = {
                    "id": "chat_test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if tools else "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            self.send_response(status)
            if stream:
                self.send_header("Content-Type", "text/event-stream")
                parts = [
                    {
                        "id": "test",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "test-model",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": None}
                        ],
                    }
                    for delta in (
                        {"reasoning_content": "NOT-PUBLIC"},
                        {"content": "O"},
                        {"content": "K"},
                    )
                ]
                if complete:
                    parts.append(
                        {
                            "id": "test",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "test-model",
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": "stop"}
                            ],
                        }
                    )
                data = (
                    "".join("data: " + json.dumps(p) + "\n\n" for p in parts)
                    + "data: [DONE]\n\n"
                ).encode()
            else:
                self.send_header("Content-Type", "application/json")
                data = json.dumps(value).encode()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def profile(protocol, base, stream=False):
    return {
        "id": "mock",
        "model_id": {
            "anthropic": "claude-sonnet-4-5",
            "gemini": "gemini-2.0-flash",
        }.get(protocol, "test-model"),
        "context": 8192,
        "max_tokens": 100,
        "timeout": 30,
        "stream": stream,
        "capabilities": ["text", "tools"],
        "provider": {"protocol": protocol, "base_url": base, "local": True},
    }


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
def test_real_sdk_provider_wire_protocol(protocol):
    with provider_server(protocol) as (base, calls):
        engine = ModelEngine(
            SimpleNamespace(key=lambda _: "unit-test-key"), asyncio.Lock()
        )
        result = asyncio.run(
            engine.text(
                profile(
                    protocol, base + ("/v1beta" if protocol == "gemini" else "/v1")
                ),
                [{"role": "user", "content": "hello"}],
            )
        )
        assert result == "OK"
        assert len(calls) == 1
        assert {
            "openai": "chat/completions",
            "anthropic": "messages",
            "gemini": "generateContent",
        }[protocol] in calls[0]["path"]


def test_real_sdk_streaming_never_displays_reasoning():
    with provider_server("openai", stream=True) as (base, _calls):
        engine = ModelEngine(
            SimpleNamespace(key=lambda _: "unit-test-key"), asyncio.Lock()
        )
        deltas = []
        assert (
            asyncio.run(
                engine.text(
                    profile("openai", base + "/v1", True),
                    [{"role": "user", "content": "hello"}],
                    deltas.append,
                )
            )
            == "OK"
        )
        assert "".join(deltas) == "OK"


def test_real_sdk_preserves_function_calls():
    with provider_server("openai", tools=True) as (base, _calls):
        engine = ModelEngine(
            SimpleNamespace(key=lambda _: "unit-test-key"), asyncio.Lock()
        )
        tool = {
            "type": "function",
            "function": {
                "name": "read",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }
        response = asyncio.run(
            engine.completion(
                profile("openai", base + "/v1"),
                [{"role": "user", "content": "read README"}],
                stream=False,
                tools=[tool],
            )
        )
        assert response.choices[0].message.tool_calls[0].function.name == "read"
        assert (
            json.loads(response.choices[0].message.tool_calls[0].function.arguments)[
                "path"
            ]
            == "README.md"
        )


def test_incomplete_stream_is_not_reported_as_complete():
    with provider_server("openai", stream=True, complete=False) as (base, calls):
        engine = ModelEngine(
            SimpleNamespace(key=lambda _: "unit-test-key"), asyncio.Lock()
        )
        with pytest.raises(RuntimeError, match="no retry"):
            asyncio.run(
                engine.text(
                    profile("openai", base + "/v1", True),
                    [{"role": "user", "content": "hello"}],
                    lambda _: None,
                )
            )
        assert len(calls) == 1


def test_model_timeout_does_not_retry(monkeypatch):
    import sys

    calls = []

    async def stalled(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(10)

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=stalled))
    engine = ModelEngine(SimpleNamespace(key=lambda _: "unit-test-key"), asyncio.Lock())
    value = profile("openai", "http://127.0.0.1:1/v1")
    value["timeout"] = 0.01
    with pytest.raises(RuntimeError, match="TimeoutError"):
        asyncio.run(engine.text(value, [{"role": "user", "content": "hello"}]))
    assert len(calls) == 1


def test_real_sdk_rate_limit_is_not_retried_or_exposed():
    with provider_server("openai", status=429) as (base, calls):
        engine = ModelEngine(
            SimpleNamespace(key=lambda _: "unit-test-key"), asyncio.Lock()
        )
        with pytest.raises(RuntimeError) as error:
            asyncio.run(
                engine.text(
                    profile("openai", base + "/v1"),
                    [{"role": "user", "content": "hello"}],
                )
            )
        assert "DO-NOT-EXPOSE-SECRET" not in str(error.value)
        assert len(calls) == 1

"""Runnable checks: python tests/test_tools.py"""

import asyncio
import json
import sys

sys.path.insert(0, ".")

from app import api
from app.tools import search

TOOL = search.TOOL


def check_tool_shape():
    fn = TOOL["function"]
    assert TOOL["type"] == "function"
    assert fn["name"] == "web_search"
    assert fn["parameters"]["required"] == ["query"]
    assert fn["description"]


def check_bad_arguments():
    assert asyncio.run(search.run("not json")) == []
    assert asyncio.run(search.run(json.dumps({"query": "  "}))) == []
    assert asyncio.run(search.run(json.dumps({}))) == []


def check_unreachable():
    search.settings.searxng_url = "http://127.0.0.1:1"
    assert asyncio.run(search.run(json.dumps({"query": "x"}))) == []


def check_time_range():
    assert search.time_range_for({"time_range": "day"}, "anything") == "day"
    assert search.time_range_for({"time_range": "bogus"}, "latest ai model") == "week"
    assert search.time_range_for({}, "harga emas hari ini") == "week"
    assert search.time_range_for({}, "what is dependency injection") == ""


def check_render():
    out = search.render([{"title": "T", "url": "http://u", "snippet": "S"}])
    assert "[1] T — http://u" in out and out.endswith("S")
    assert search.render([]) == "No results."


def check_run_accepts_dict():
    search.settings.searxng_url = "http://127.0.0.1:1"
    assert asyncio.run(search.run({"query": "x"})) == []
    assert asyncio.run(search.run("not json")) == []


def check_native_stream():
    """Ollama /api/chat streams JSON lines with a message object per chunk."""
    lines = [
        json.dumps({"message": {"role": "assistant", "thinking": "hmm"}}),
        json.dumps({"message": {"role": "assistant", "content": "hal"}}),
        json.dumps({"message": {"role": "assistant", "content": "o"}}),
        json.dumps({"message": {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "web_search", "arguments": {"query": "eris"}}}]}, "done": True}),
        json.dumps({"model": "x", "message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    class FakeResp:
        status_code = 200

        async def aiter_lines(self):
            for line in lines:
                yield line

        async def aread(self):
            return b""

    class FakeStream:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kw):
            assert url == "http://fake/api/chat", url
            assert kw["json"]["messages"] == [{"role": "user", "content": "hi"}]
            return FakeStream()

    api.httpx.AsyncClient = FakeClient
    api.settings.model_base_url = "http://fake/v1"

    async def run():
        return [item async for item in api.stream_completion([{"role": "user", "content": "hi"}])]

    out = asyncio.run(run())
    assert out[:3] == [("thinking", "hmm"), ("delta", "hal"), ("delta", "o")], out
    assert out[3] == ("tool_calls", [{"id": "c1", "function": {"name": "web_search", "arguments": {"query": "eris"}}}]), out


def check_to_native():
    mm = [{"role": "user", "content": [{"type": "text", "text": "hello "}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}]
    out = api.to_native(mm)
    assert out == [{"role": "user", "content": "hello ", "images": ["AAA"]}], out
    plain = [{"role": "assistant", "content": "x"}]
    assert api.to_native(plain) == plain


for name, fn in sorted(globals().items()):
    if name.startswith("check_"):
        fn()
        print(f"ok  {name}")
print("all checks passed")
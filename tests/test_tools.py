"""Runnable checks: python tests/test_tools.py"""

import asyncio
import json
import sys

sys.path.insert(0, ".")

from datetime import datetime, timedelta, timezone

from app import api
from app.tools import calc, doc_search, fetch, search

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
    assert "Do NOT answer from memory" in search.render([])


def check_run_accepts_dict():
    search.settings.searxng_url = "http://127.0.0.1:1"
    assert asyncio.run(search.run({"query": "x"})) == []
    assert asyncio.run(search.run("not json")) == []


def check_rank():
    hits = [
        {"title": "Some blog", "url": "http://blog.example/x", "snippet": "s"},
        {"title": "GPT-6 Astra announcement - OpenAI", "url": "https://openai.com/index/gpt-6-astra", "snippet": "s"},
        {"title": "gpt 6 astra discussion", "url": "https://facebook.com/post/1", "snippet": "s"},
    ]
    out = search.rank("gpt 6 astra", hits)
    assert out[0]["url"].startswith("https://openai.com"), out
    assert out[1]["url"] == "http://blog.example/x", out
    assert out[2]["url"] == "https://facebook.com/post/1", out


def check_rank_freshness_dedup():
    now = datetime.now(timezone.utc)
    old_d = (now - timedelta(days=60)).isoformat()
    new_d = (now - timedelta(days=1)).isoformat()
    hits = [
        {"title": "gpt news", "url": "https://a.com/1", "snippet": "", "published": old_d},
        {"title": "gpt news", "url": "https://b.com/2", "snippet": "", "published": new_d},
        {"title": "gpt blog", "url": "https://c.com/x?utm_source=tw", "snippet": ""},
        {"title": "gpt blog dup", "url": "https://c.com/x", "snippet": ""},
        {"title": "gpt d1", "url": "https://d.com/1", "snippet": ""},
        {"title": "gpt d2", "url": "https://d.com/2", "snippet": ""},
        {"title": "gpt d3", "url": "https://d.com/3", "snippet": ""},
    ]
    out = search.rank("gpt news", hits, fresh=True)
    urls = [r["url"] for r in out]
    assert "https://c.com/x" in urls and "https://c.com/x?utm_source=tw" not in urls, urls
    assert sum(u.startswith("https://d.com") for u in urls) == 2, urls
    assert out[0]["url"] == "https://b.com/2", urls
    # without fresh, recency must not matter between same-score pages
    out2 = search.rank("gpt news", hits, fresh=False)
    assert out2[0]["url"] == "https://a.com/1", out2


def check_fetch_tool_shape():
    fn = fetch.TOOL["function"]
    assert fn["name"] == "fetch_url"
    assert fn["parameters"]["required"] == ["url"]
    assert asyncio.run(fetch.run("not json")) == "No URL given."
    # SSRF: private address never fetched, message says so
    assert "Could not read" in asyncio.run(fetch.run({"url": "http://127.0.0.1:8000/"}))


def check_numbering():
    out = search.render([{"title": "A", "url": "http://a", "snippet": "s"}], start=4)
    assert "[5] A" in out, out
    docs = doc_search.render([{"source": "f.txt", "text": "isi"}], start=4)
    assert "[5] (f.txt)" in docs, docs


def check_drop_last_assistant():
    import os
    import tempfile
    from app import chats

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["CHAT_DB"] = path
    chats._db = None

    chats.save_turn(1, "c1", "user", "q")
    chats.save_turn(1, "c1", "assistant", "ans1")
    chats.save_turn(1, "c1", "user", "q2")
    chats.save_turn(1, "c1", "assistant", "ans2")

    chats.drop_last_assistant(1, "c1")
    turns = chats.turns(1, "c1")
    assert [x["content"] for x in turns] == ["q", "ans1", "q2"], turns

    chats.drop_last_assistant(1, "c1")
    assert [x["content"] for x in chats.turns(1, "c1")] == ["q", "q2"], chats.turns(1, "c1")
    chats.drop_last_assistant(1, "c1")
    assert [x["content"] for x in chats.turns(1, "c1")] == ["q", "q2"], chats.turns(1, "c1")
    chats.drop_last_assistant(9, "nope")

    chats._db.close()
    chats._db = None
    del os.environ["CHAT_DB"]
    os.unlink(path)


def check_ssrf_guard():
    from app.tools import fetch

    for bad in (
        "http://127.0.0.1/x",
        "http://localhost:8000/x",
        "http://10.0.0.5/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://172.16.3.2/x",
        "http://[::1]/x",
        "ftp://8.8.8.8/x",
        "http:///",
    ):
        assert not fetch.is_public_url(bad), bad
    assert fetch.is_public_url("http://8.8.8.8/x")


def check_extract_text():
    from app.tools import fetch

    html = (
        "<html><head><title>T</title><style>.a{color:red}</style></head>"
        "<body><script>var x=1;</script><h1>Hello</h1><p>  world   again </p></body></html>"
    )
    text = fetch.extract_text(html, 100)
    assert text == "Hello world again", repr(text)
    assert fetch.extract_text(html, 5) == "Hello"[:5]

    page = (
        "<html><head><title>T</title></head><body><header>menu menu</header>"
        "<main><p>real content here</p></main><footer>copyright</footer></body></html>"
    )
    assert fetch.extract_text(page, 100) == "real content here"


def check_render_page_content():
    out = search.render([{"title": "T", "url": "http://u", "snippet": "S", "page": "full body text"}])
    assert "Page content: full body text" in out
    assert "http://u" in out

    failed = search.render([{"title": "T", "url": "http://u", "snippet": "S", "page": None}])
    assert "could not be read" in failed
    skipped = search.render([{"title": "T", "url": "http://u", "snippet": "S"}])
    assert "could not be read" not in skipped


def check_calc():
    assert asyncio.run(calc.run({"expression": "2*1000000 + 6*3000000"})) == "2*1000000 + 6*3000000 = 20000000"
    assert "= 2.5" in asyncio.run(calc.run({"expression": "5/2"}))
    assert "cannot evaluate" in asyncio.run(calc.run({"expression": "__import__('os').system('echo x')"}))
    assert "cannot evaluate" in asyncio.run(calc.run({"expression": "'abc'}"}))
    assert "cannot evaluate" in asyncio.run(calc.run({"expression": "2**9999999"}))
    assert "cannot evaluate" in asyncio.run(calc.run({"expression": "1/0"}))
    assert "cannot evaluate" in asyncio.run(calc.run({"expression": "9**9**9"}))
    assert "= 81" in asyncio.run(calc.run({"expression": "(1+2)**(2+2)"}))
    assert "no expression" in asyncio.run(calc.run({}))


def check_doc_search_shape():
    fn = doc_search.TOOL["function"]
    assert fn["name"] == "doc_search"
    assert fn["parameters"]["required"] == ["query"]
    assert "do not invent" in doc_search.render([])
    out = doc_search.render([{"source": "a.txt", "text": "halo"}])
    assert "[1] (a.txt)" in out

    from app import api as api_mod
    assert api_mod.TOOL_NAMES == {"web_search", "fetch_url", "calc", "doc_search"}


def check_rag_per_user():
    import inspect

    from app import rag

    assert not hasattr(rag, "chat_collection")
    assert "user_id" in inspect.signature(rag.add_document).parameters
    assert "user_id" in inspect.signature(rag.retrieve).parameters


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


def check_chats_sqlite():
    import os
    import tempfile
    from app import chats

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["CHAT_DB"] = path
    chats._db = None

    chats.save_turn(1, "c1", "user", "halo dunia")
    chats.touch_title("c1", "halo dunia")
    chats.save_turn(1, "c1", "assistant", "hai", thinking="t" * 9000,
                    sources=[{"source": "a", "kind": "doc"}] * 10,
                    web=[{"query": "q1", "items": [{"source": "http://u", "kind": "web"}]}])
    chats.save_turn(2, "c2", "user", "lain")

    items = chats.list_chats(1)
    assert [c["id"] for c in items] == ["c1"], items
    assert items[0]["title"] == "halo dunia"[:40]

    turns = chats.turns(1, "c1")
    assert len(turns) == 2 and turns[0]["role"] == "user"
    assert len(turns[1]["thinking"]) == 8000
    assert len(turns[1]["sources"]) == 8
    assert turns[1]["web"][0]["query"] == "q1"

    chats.save_turn(1, "c1", "assistant", "hal", thinking=["satu", "dua"])
    last = chats.turns(1, "c1")[-1]
    assert last["thinking"] == ["satu", "dua"], last
    assert chats.turns(2, "c1") == []

    chats.set_meta(1, "c1", title="baru", pinned=True)
    assert chats.list_chats(1)[0] == {"id": "c1", "title": "baru", "pinned": True}
    chats.save_turn(1, "c3", "user", "pin juga")
    chats.set_meta(1, "c3", pinned=True)
    ordered = chats.list_chats(1)
    assert [c["id"] for c in ordered] == ["c3", "c1"], ordered

    chats.delete(1, "c1")
    assert chats.list_chats(1) == [{"id": "c3", "title": "New chat", "pinned": True}]

    chats._db.close()
    chats._db = None
    del os.environ["CHAT_DB"]
    os.unlink(path)


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
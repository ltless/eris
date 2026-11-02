"""Runnable checks: python tests/test_tools.py"""

import asyncio
import json
import sys

sys.path.insert(0, ".")

from datetime import datetime, timedelta, timezone

from app import api
from app.tools import fetch, search

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
    prev = search.settings.searxng_url
    search.settings.searxng_url = "http://127.0.0.1:1"
    try:
        assert asyncio.run(search.run(json.dumps({"query": "x"}))) == []
    finally:
        search.settings.searxng_url = prev


def check_time_range():
    assert search.time_range_for({"time_range": "day"}, "anything") == "day"
    assert search.time_range_for({"time_range": "bogus"}, "latest ai model") == "week"
    assert search.time_range_for({}, "harga emas hari ini") == "week"
    assert search.time_range_for({}, "what is dependency injection") == ""
    # What the small models actually send: quotes kept on, or two options run
    # together. Both were refused by the schema before they ever reached SearXNG.
    assert search.time_range_for({"time_range": '"day"'}, "anything") == "day"
    assert search.time_range_for({"time_range": " YEAR "}, "anything") == "year"


def _capturing_client(sent):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            sent.update(params or {})
            return FakeResp()

    return FakeClient


def check_enum_slips_reach_searxng():
    """A stray value is a dropped argument, never a refused call."""
    from app.tools import builtin
    from app.tools.registry import registry

    sent = {}
    old_client = search.httpx.AsyncClient
    search.httpx.AsyncClient = _capturing_client(sent)
    try:
        builtin.register_builtins()
        r = asyncio.run(registry.call(
            "web_search",
            {"query": "gold price", "categories": "generalnews", "time_range": '"day"'},
        ))
    finally:
        search.httpx.AsyncClient = old_client
    assert r.success, r.error
    assert sent["categories"] == "news", sent
    assert sent["time_range"] == "day", sent


def _scripted_client(replies):
    """A client that answers each request with the next payload in `replies`
    and records every parameter set it was sent."""
    sent = []

    class FakeResp:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            sent.append(dict(params or {}))
            body = replies[min(len(sent), len(replies)) - 1]
            return FakeResp(body)

    return FakeClient, sent


HIT = {"results": [{"title": "Bitcoin price", "url": "https://example.com/b",
                    "content": "around 78k"}]}


def check_filters_are_retried_wider():
    """A news-and-a-day filter that returns nothing is retried without them.

    The model asks for `categories: news` and `time_range: day` on nearly every
    current-events question, and that pair is the thinnest slice of the index.
    An empty answer from it is not "the web has nothing"."""
    FakeClient, sent = _scripted_client([{"results": []}, HIT])
    old, old_pages = search.httpx.AsyncClient, search.settings.fetch_pages
    search.httpx.AsyncClient = FakeClient
    # The fake client only speaks the search endpoint; a top hit would otherwise
    # send the run on to read pages it cannot read.
    search.settings.fetch_pages = False
    try:
        hits, reason = asyncio.run(search.run_checked(
            {"query": "bitcoin price", "categories": "news", "time_range": "day"}))
    finally:
        search.httpx.AsyncClient, search.settings.fetch_pages = old, old_pages
    assert reason == "", reason
    assert [h["url"] for h in hits] == ["https://example.com/b"], hits
    assert sent[0]["categories"] == "news" and sent[0]["time_range"] == "day", sent
    assert "categories" not in sent[1] and "time_range" not in sent[1], sent


DOWN_NEWS = {"results": [], "unresponsive_engines": [
    ["bing news", "Suspended: too many requests"],
    ["reuters", "CAPTCHA"],
]}


def check_a_down_news_stack_is_not_a_failed_search():
    """The live case: `categories=news` with `time_range=day` came back empty
    with the news engines suspended, and the failure was reported without ever
    asking the general stack, which answers the same query with 30 rows.

    The unresponsive list describes the engines behind the categories asked
    for. Their being down says nothing about the engines behind the query."""
    FakeClient, sent = _scripted_client([DOWN_NEWS, HIT])
    old, old_pages = search.httpx.AsyncClient, search.settings.fetch_pages
    search.httpx.AsyncClient = FakeClient
    search.settings.fetch_pages = False
    try:
        hits, reason = asyncio.run(search.run_checked(
            {"query": "jakarta weather", "categories": "news", "time_range": "day"}))
    finally:
        search.httpx.AsyncClient, search.settings.fetch_pages = old, old_pages
    assert reason == "", reason
    assert [h["url"] for h in hits] == ["https://example.com/b"], hits
    assert len(sent) == 2, sent


def check_suspended_engines_are_a_failure():
    """Every engine suspended still answers 200 with no rows. Saying "found
    nothing" there is what sent the model off to answer from memory."""
    body = {"results": [], "unresponsive_engines": [
        ["brave", "Suspended: too many requests"],
        ["duckduckgo", "CAPTCHA"],
    ]}
    FakeClient, sent = _scripted_client([body])
    old = search.httpx.AsyncClient
    search.httpx.AsyncClient = FakeClient
    try:
        hits, reason = asyncio.run(search.run_checked({"query": "bitcoin price"}))
    finally:
        search.httpx.AsyncClient = old
    assert hits == [], hits
    assert reason.startswith("search engines unavailable"), reason
    assert "brave" in reason and "duckduckgo" in reason, reason
    # "bitcoin price" reads as a fresh query, so the refusal is the one that
    # comes back after the filters are dropped, not the filtered one.
    assert len(sent) == 2, sent
    assert "time_range" not in sent[-1], sent


def check_render():
    out = search.render([{"title": "T", "url": "http://u", "snippet": "S"}])
    assert "[1] T — http://u" in out and out.endswith("S")
    assert "Do NOT answer from memory" in search.render([])


def check_query_cleaning():
    # a date pasted into the query is what killed the engine's result set
    assert search.clean_query("weather in Bali today 2026-10-19") == "weather in Bali today"
    assert search.clean_query("harga emas  19/10/2026 ") == "harga emas"
    assert search.clean_query("gold price 2026.10.19") == "gold price"
    # plain queries come back untouched, dates that are already prose stay
    assert search.clean_query("dependency injection python") == "dependency injection python"
    assert search.clean_query("Bali weather today October 2026") == "Bali weather today October 2026"
    assert search.clean_query("version 2.5.1 release") == "version 2.5.1 release"
    # never clean a query down to nothing
    assert search.clean_query("2026-10-19") == "2026-10-19"


def check_run_sends_cleaned_query():
    sent = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            sent.update(params or {})
            return FakeResp()

    old = search.httpx.AsyncClient
    search.httpx.AsyncClient = FakeClient
    try:
        asyncio.run(search.run({"query": "weather in Bali today 2026-10-19"}))
    finally:
        search.httpx.AsyncClient = old
    assert sent["q"] == "weather in Bali today", sent
    assert sent["time_range"] == "week", sent


def check_run_accepts_dict():
    prev = search.settings.searxng_url
    search.settings.searxng_url = "http://127.0.0.1:1"
    try:
        assert asyncio.run(search.run({"query": "x"})) == []
        assert asyncio.run(search.run("not json")) == []
        assert asyncio.run(search.run(["x"])) == []
    finally:
        search.settings.searxng_url = prev


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
    # The short reason travels apart from the sentence, so the card can replay
    # it after a reload without restating the URL it already shows.
    ok, text, reason = asyncio.run(fetch.read({"url": "http://127.0.0.1:8000/"}))
    assert not ok and reason == "not a public http(s) address", reason
    assert text.startswith("Could not read"), text
    ok, text, reason = asyncio.run(fetch.read({}))
    assert not ok and reason and text == "No URL given."


def check_numbering():
    out = search.render([{"title": "A", "url": "http://a", "snippet": "s"}], start=4)
    assert "[5] A" in out, out


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
        "http://[::ffff:127.0.0.1]/x",
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


def check_registry_chat_tools():
    from app.tools.registry import registry
    chat_tools = {t.name for t in registry.all() if "chat" in t.tags}
    assert chat_tools == {"web_search", "fetch_url"}


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

    prev_client, prev_base = api.httpx.AsyncClient, api.settings.model_base_url
    api.httpx.AsyncClient = FakeClient
    api.settings.model_base_url = "http://fake/v1"

    async def run():
        return [item async for item in api.stream_completion([{"role": "user", "content": "hi"}])]

    try:
        out = asyncio.run(run())
    finally:
        api.httpx.AsyncClient = prev_client
        api.settings.model_base_url = prev_base
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
                    sources=[{"source": "a", "kind": "web"}] * 10,
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


def check_quote_roundtrip():
    import os
    import tempfile
    from app import chats

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["CHAT_DB"] = path
    chats._db = None

    chats.save_turn(1, "c1", "user", "apa ini", quote="kalimat yang dipilih")
    # A reply can be the selection alone, with nothing typed under it.
    chats.save_turn(1, "c1", "user", "", quote="cuma kutipan")
    chats.save_turn(1, "c1", "user", "tanpa kutipan")

    turns = chats.turns(1, "c1")
    assert turns[0]["quote"] == "kalimat yang dipilih", turns[0]
    assert turns[0]["content"] == "apa ini"
    assert turns[1]["quote"] == "cuma kutipan", turns[1]
    assert turns[1]["content"] == ""
    assert "quote" not in turns[2], turns[2]

    # The model reads the quote as markdown above the question, never as its own
    # field, and a quote with no question still reaches it.
    assert api._with_quote("apa ini", "kalimat yang dipilih") == (
        "> kalimat yang dipilih\n\napa ini"
    )
    assert api._with_quote("", "cuma kutipan") == "> cuma kutipan"
    assert api._with_quote("apa ini", "") == "apa ini"

    chats._db.close()
    chats._db = None
    del os.environ["CHAT_DB"]
    os.unlink(path)


def check_images_roundtrip():
    import base64
    import os
    import tempfile
    from app import chats

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["CHAT_DB"] = path
    chats._db = None

    jpeg = b"\xff\xd8\xff" + b"body" * 50
    png = b"\x89PNG\r\n\x1a\n" + b"pixels" * 50
    url = lambda raw, mime: f"data:{mime};base64,{base64.b64encode(raw).decode()}"

    # A turn can be nothing but an image.
    chats.save_turn(1, "c1", "user", "", images=[jpeg, png])
    turn = chats.turns(1, "c1")[0]
    assert turn["content"] == ""
    assert [u.split(",")[0] for u in turn["images"]] == ["data:image/jpeg;base64", "data:image/png;base64"]
    assert turn["images"][0] == url(jpeg, "image/jpeg")

    # The type comes from the bytes, not from what the data URL claimed.
    assert chats.decode_data_url(url(jpeg, "image/png")) == jpeg
    assert chats.decode_data_url("https://example.com/a.png") is None
    assert chats.decode_data_url("data:image/png;base64,!!!!") is None
    assert chats.decode_data_url("data:image/png,notbase64") is None

    # Something that is not an image is dropped rather than stored as one.
    assert chats.pack_images([b"not an image"]) is None
    assert chats.pack_images([jpeg, b"not an image"]) is not None

    # The cap is on the bytes, and it stops rather than overruns.
    chats.MAX_IMAGE_BYTES = len(jpeg) + 10
    try:
        blob = chats.pack_images([jpeg, jpeg])
        assert chats.unpack_images(blob) == [url(jpeg, "image/jpeg")], blob
    finally:
        chats.MAX_IMAGE_BYTES = 4_000_000

    # A turn with no images carries no column and no key.
    chats.save_turn(1, "c1", "user", "text only")
    assert "images" not in chats.turns(1, "c1")[-1]

    chats._db.close()
    chats._db = None
    del os.environ["CHAT_DB"]
    os.unlink(path)


def check_files_roundtrip():
    import os
    import tempfile
    from app import api, chats

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["CHAT_DB"] = path
    chats._db = None

    files = [{"name": "notes.txt", "text": "hello\nworld"}, {"name": "a.py", "text": "print(1)"}]
    # A turn can be nothing but a file.
    chats.save_turn(1, "c1", "user", "", files=files)
    turn = chats.turns(1, "c1")[0]
    assert turn["files"] == files, turn
    assert turn["content"] == ""

    # The model reads each file under its own name, in a fence.
    joined = api._with_files("what is this?", files)
    assert "### File: notes.txt\n```txt\nhello\nworld\n```" in joined, joined
    assert "what is this?" in joined and joined.endswith("what is this?")
    assert api._with_files("", None) == ""

    # A file alone still carries a turn to the model, and names the chat.
    cleaned = api._clean_files([{"name": "x.md", "text": "a" * 90_000}])
    assert cleaned[0]["name"] == "x.md"
    assert cleaned[0]["text"].endswith("[... truncated]")
    assert len(cleaned[0]["text"]) <= api.MAX_FILE_CHARS + len("\n[... truncated]")
    assert api._clean_files([]) is None
    assert api._clean_files(None) is None

    chats.save_turn(1, "c1", "user", "text only")
    assert "files" not in chats.turns(1, "c1")[-1]

    chats._db.close()
    chats._db = None
    del os.environ["CHAT_DB"]
    os.unlink(path)


def check_chats_ownership():
    import os
    import tempfile
    from app import chats

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["CHAT_DB"] = path
    chats._db = None

    chats.save_turn(1, "mine", "user", "q")
    chats.save_turn(2, "theirs", "user", "q2")

    # a turn may not be written into a chat somebody else owns
    chats.save_turn(1, "theirs", "user", "injected")
    assert [t["content"] for t in chats.turns(2, "theirs")] == ["q2"]

    # deleting a foreign chat must leave its turns alone
    chats.delete(1, "theirs")
    assert [t["content"] for t in chats.turns(2, "theirs")] == ["q2"]
    assert [c["id"] for c in chats.list_chats(2)] == ["theirs"]

    # thinking text that merely starts with "[" is not a JSON list
    chats.save_turn(1, "mine", "assistant", "a", thinking="[not json at all")
    assert chats.turns(1, "mine")[-1]["thinking"] == "[not json at all"

    # a long multi-round thinking list stays valid JSON and keeps its shape
    chats.save_turn(1, "mine", "assistant", "b", thinking=["x" * 3000] * 6)
    stored = chats.turns(1, "mine")[-1]["thinking"]
    assert isinstance(stored, list) and stored, stored
    assert sum(len(p) for p in stored) <= chats.MAX_THINKING

    chats._db.close()
    chats._db = None
    del os.environ["CHAT_DB"]
    os.unlink(path)


def check_turns_window():
    """A long chat opens on its newest turns, still in reading order."""
    import os
    import tempfile
    from app import chats

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["CHAT_DB"] = path
    chats._db = None

    for i in range(5):
        chats.save_turn(1, "c", "user", f"m{i}")
    window = chats.turns(1, "c", limit=3)
    assert [t["content"] for t in window] == ["m2", "m3", "m4"], window

    chats._db.close()
    chats._db = None
    del os.environ["CHAT_DB"]
    os.unlink(path)


def check_rank_keeps_input():
    hits = [{"title": "t", "url": "https://a.com/x?utm_source=z", "snippet": ""}]
    search.rank("t", hits)
    assert hits[0]["url"] == "https://a.com/x?utm_source=z", hits


def check_to_native():
    mm = [{"role": "user", "content": [{"type": "text", "text": "hello "}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}]
    out = api.to_native(mm)
    assert out == [{"role": "user", "content": "hello ", "images": ["AAA"]}], out
    plain = [{"role": "assistant", "content": "x"}]
    assert api.to_native(plain) == plain


def check_favicons_refuse_a_bad_domain():
    """The domain goes into a URL, so anything that is not a plain hostname is
    refused before a request is made: a path, a port, or a scheme would let the
    query string pick the address."""
    from app import favicons

    for bad in ("", "..", "a..b", "localhost:8080", "x/y", "http://x.com", "-x.com"):
        assert asyncio.run(favicons.icon_for(bad)) is None, bad


def check_favicon_is_read_from_the_site_itself():
    """The icon comes from the domain, not a third-party service, and a site
    that answers with something other than an image is a miss."""
    from app import favicons

    got = []

    class Resp:
        def __init__(self, status, ctype, body=b""):
            self.status_code = status
            self.headers = {"content-type": ctype} if ctype else {}
            self.body = body

        async def aiter_bytes(self):
            yield self.body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Client:
        def __init__(self, *a, **k):
            self.next = Resp(200, "image/x-icon", b"\x00\x00\x01\x00")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url):
            got.append(url)
            return self.next

    old_client, old_guard = favicons.httpx.AsyncClient, favicons.is_public_url
    favicons.httpx.AsyncClient = Client
    favicons.is_public_url = lambda u: True
    try:
        raw, ctype = asyncio.run(favicons.icon_for("Example.com."))
        assert got == ["https://example.com/favicon.ico"], got
        assert raw.startswith(b"\x00\x00\x01\x00") and ctype == "image/x-icon", (raw, ctype)

        # Cached: the second ask does not go out again.
        asyncio.run(favicons.icon_for("example.com"))
        assert len(got) == 1, got

        c = Client()
        c.next = Resp(200, "text/html", b"<html>")
        favicons.httpx.AsyncClient = lambda *a, **k: c
        assert asyncio.run(favicons.icon_for("html.example")) is None
    finally:
        favicons.httpx.AsyncClient, favicons.is_public_url = old_client, old_guard
        favicons._cache.clear()


def check_favicon_guard_runs_per_hop():
    """A public host may redirect somewhere private; the guard is checked on
    every hop, not once at the start."""
    from app import favicons

    seen = []

    class Resp:
        def __init__(self, status, loc=None):
            self.status_code = status
            self.headers = {"location": loc} if loc else {}

        async def aiter_bytes(self):
            yield b""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url):
            seen.append(url)
            return Resp(302, "http://127.0.0.1/favicon.ico")

    def guard(url):
        return "127.0.0.1" not in url

    old_client, old_guard = favicons.httpx.AsyncClient, favicons.is_public_url
    favicons.httpx.AsyncClient = Client
    favicons.is_public_url = guard
    try:
        assert asyncio.run(favicons.icon_for("redirect.example")) is None
        # The fallback asks the page too, so the hops run past one request; what
        # has to hold is that no blocked hop was ever reached.
        assert seen[0] == "https://redirect.example/favicon.ico", seen
        assert not any("127.0.0.1" in u for u in seen), seen
    finally:
        favicons.httpx.AsyncClient, favicons.is_public_url = old_client, old_guard
        favicons._cache.clear()


def check_favicon_falls_back_to_the_page_s_own_link():
    """A site that serves no `/favicon.ico` usually names its icon in the page.

    Only the sites that miss the direct path cost the extra request, and the
    URL that comes back is whatever the page declared, resolved against the
    page rather than against the favicon path.
    """
    from app import favicons

    HOME = (
        b'<html><head><link rel="shortcut icon" href="/assets/icon.png">'
        b'<link rel="apple-touch-icon" href="https://cdn.example/touch.png">'
        b'<link rel="stylesheet" href="/style.css"></head></html>'
    )

    class Resp:
        def __init__(self, status, ctype, body=b""):
            self.status_code = status
            self.headers = {"content-type": ctype} if ctype else {}
            self.body = body

        async def aiter_bytes(self):
            yield self.body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url):
            asked.append(url)
            if url.endswith("/favicon.ico"):
                return Resp(404, "text/html", b"not found")
            if url == "https://page.example/":
                return Resp(200, "text/html; charset=utf-8", HOME)
            if url == "https://page.example/assets/icon.png":
                return Resp(200, "image/png", b"\x89PNG")
            return Resp(404, "text/html", b"nope")

    asked = []
    old_client, old_guard = favicons.httpx.AsyncClient, favicons.is_public_url
    favicons.httpx.AsyncClient = Client
    favicons.is_public_url = lambda u: True
    try:
        raw, ctype = asyncio.run(favicons.icon_for("page.example"))
        assert (raw, ctype) == (b"\x89PNG", "image/png"), (raw, ctype)
        assert asked == [
            "https://page.example/favicon.ico",
            "https://page.example/",
            "https://page.example/assets/icon.png",
        ], asked

        # A declared icon that is not an image is not an icon.
        c = Client()
        c.stream = lambda m, u: (
            Resp(404, "text/html", b"x") if u.endswith("/favicon.ico")
            else Resp(200, "text/html", HOME) if u == "https://page.example/"
            else Resp(200, "text/html", b"<html>")
        )
        favicons.httpx.AsyncClient = lambda *a, **k: c
        favicons._cache.clear()
        assert asyncio.run(favicons.icon_for("page.example")) is None
    finally:
        favicons.httpx.AsyncClient, favicons.is_public_url = old_client, old_guard
        favicons._cache.clear()


def check_a_redirected_host_is_asked_at_its_new_origin():
    """A bare host that redirects home is followed, and the icon is looked for
    on the origin it landed on — `github.io` is one, and its icon is not on
    `github.io` at all."""
    from app import favicons

    class Resp:
        def __init__(self, status, ctype=None, body=b"", loc=None):
            self.status_code = status
            self.headers = {}
            if ctype:
                self.headers["content-type"] = ctype
            if loc:
                self.headers["location"] = loc
            self.body = body

        async def aiter_bytes(self):
            yield self.body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url):
            asked.append(url)
            if url == "https://bare.example/favicon.ico":
                return Resp(404, "text/html", b"x")
            if url == "https://bare.example/":
                return Resp(301, loc="https://home.example/landing")
            if url == "https://home.example/favicon.ico":
                return Resp(200, "image/x-icon", b"\x00\x00\x01\x00")
            if url == "https://home.example/landing":
                return Resp(200, "text/html", b"<html><head></head></html>")
            return Resp(404, "text/html", b"x")

    asked = []
    old_client, old_guard = favicons.httpx.AsyncClient, favicons.is_public_url
    favicons.httpx.AsyncClient = Client
    favicons.is_public_url = lambda u: True
    try:
        raw, ctype = asyncio.run(favicons.icon_for("bare.example"))
        assert ctype == "image/x-icon", (raw, ctype)
        assert "https://home.example/favicon.ico" in asked, asked
    finally:
        favicons.httpx.AsyncClient, favicons.is_public_url = old_client, old_guard
        favicons._cache.clear()


def check_declared_icons_are_resolved_and_filtered():
    """The listed URLs come out absolute and http(s), in the page's own order."""
    from app import favicons

    page = (
        b'<html><head>'
        b'<link rel="icon" href="data:image/png;base64,AAA">'
        b'<link rel="icon" href="/a.png">'
        b'<link rel="ICON" href="https://other.example/b.svg">'
        b'<link rel="stylesheet" href="/c.css">'
        b'<link rel="icon" href="/a.png">'
        b'</head></html>'
    )
    got = favicons._declared(page, "https://base.example/x/y")
    assert got == ["https://base.example/a.png", "https://other.example/b.svg"], got
    assert favicons._declared(b"<html></html>", "https://base.example/") == []
    # A page is a wall of untrusted markup; the reader has to survive a
    # relative base and an href it cannot parse.
    assert favicons._declared(b'<link rel="icon" href="::bogus">', "not a url") == []


def check_title_parsing():
    """What the title call answers with is not always the title.

    Every string on the left is a way a model has wrapped a three-word name.
    The right side is what has to reach the sidebar.
    """
    from app.models import providers

    def run(text):
        seen = []

        async def fake(messages, model=None, thinking=False, tools=None, ctx=None, limit=None):
            seen.append(limit)
            for ch in text:
                yield ("delta", ch)

        old, providers.stream_completion = providers.stream_completion, fake
        try:
            return asyncio.run(providers.title_for("how do I validate an email")), seen
        finally:
            providers.stream_completion = old

    cases = [
        ("## Email Validation", "Email Validation"),
        ("- Email Validation", "Email Validation"),
        ("1. Email Validation", "Email Validation"),
        ('Title: "Email Validation"', "Email Validation"),
        ("**Email Validation**", "Email Validation"),
        ("```\nEmail Validation\n```", "Email Validation"),
        ("Judul: Validasi Email", "Validasi Email"),
        # The label usually lands on its own line, with the name under it.
        ("Title:\n## Email Validation", "Email Validation"),
        ("Here is the title:\n\nEmail Validation", "Email Validation"),
        # A colon inside a name is part of the name.
        ("Email Validation: A Guide", "Email Validation: A Guide"),
    ]
    for raw, want in cases:
        got, cap = run(raw)
        assert got == want, (raw, got, want)
        # The name is a phrase, so the call is capped well under a paragraph.
        assert cap == [providers.TITLE_LIMIT], (raw, cap)

    # Nothing to name with: an empty answer, a bare label, or decoration only.
    # The caller falls back to the message's own opening words.
    for raw in ("", "   ", "Title", "###", "-", "```"):
        got, _ = run(raw)
        assert got == "", (raw, got)


def check_a_chosen_name_outranks_the_models():
    """The model's name is derived from the first message and races the user,
    who can rename the chat while the answer is still streaming."""
    import os
    import tempfile
    from app import chats

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["CHAT_DB"] = path
    chats._db = None

    def title_of(chat_id):
        return next((c["title"] for c in chats.list_chats(1) if c["id"] == chat_id), None)

    try:
        source = "halo dunia, apa kabar"
        chats.save_turn(1, "c1", "user", source)
        assert chats.untitled("c1", 1)
        # The model's name replaces the opening words the first message wrote;
        # those are the placeholder, not a name someone chose.
        chats.touch_title("c1", source, 1)
        assert title_of("c1") == source[:40]
        assert chats.name_from_model(1, "c1", "Judul Model", source)
        assert title_of("c1") == "Judul Model"

        # A name someone chose is not replaced by a later turn's model name.
        chats.set_meta(1, "c1", title="Rename Sendiri", pinned=False)
        assert not chats.name_from_model(1, "c1", "Judul Model 2", source)
        assert title_of("c1") == "Rename Sendiri"

        # On a chat nobody has named yet, the write is what names it.
        chats.save_turn(1, "c2", "user", "pertanyaan lain")
        assert chats.untitled("c2", 1)
        assert chats.name_from_model(1, "c2", "Judul Model", "pertanyaan lain")
        assert title_of("c2") == "Judul Model"

        # Another account's chat is not written to, and an empty name is not a
        # name either way.
        assert not chats.name_from_model(9, "c2", "Bukan Punyamu", "pertanyaan lain")
        assert title_of("c2") == "Judul Model"
        chats.set_meta(1, "c2", title="   ")
        assert title_of("c2") == chats.UNTITLED
    finally:
        chats._db.close()
        chats._db = None
        del os.environ["CHAT_DB"]
        os.unlink(path)


def check_the_opening_words_cut_at_a_word():
    from app import chats

    assert chats.short_title("halo dunia") == "halo dunia"
    # Cut on the boundary instead of mid-word, so the sidebar does not read as
    # broken text.
    long = "b" * 100
    # Cut on the boundary instead of mid-word, so the sidebar does not read as
    # broken text.
    assert chats.short_title("b" * 30 + " " + long) == "b" * 30
    assert chats.short_title("halo dunia " + long) == "halo dunia " + "b" * 29
    # A boundary near the start would throw away most of the title, so a word
    # that fills the whole limit is cut where it falls rather than dropped.
    assert chats.short_title("aaa " + long) == ("aaa " + "b" * 36)
    # Whitespace collapses: a title is one line, whatever was pasted into it.
    assert chats.short_title("a\n\nb\tc") == "a b c"


for name, fn in sorted(globals().items()):
    if name.startswith("check_"):
        fn()
        print(f"ok  {name}")
print("all checks passed")
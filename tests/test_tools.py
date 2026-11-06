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


def check_a_read_page_is_not_asked_for_again():
    """A result that already carries its page text says so, or the model spends
    a round fetching what the search handed it in the first place."""
    plain = search.render([{"title": "A", "url": "http://a", "snippet": "s"}])
    assert "already read in full" not in plain, plain
    read = search.render([{"title": "A", "url": "http://a", "snippet": "s", "page": "body"}])
    assert "already read in full" in read, read


def check_a_fetched_page_is_cached():
    """The same URL read twice inside the window costs one request, which is
    what a repeated query and a re-fetch of a top result both come down to."""
    from app.tools import fetch

    class Resp:
        status_code = 200
        headers = {"content-type": "text/html"}

        def __init__(self, body):
            self._body = body
            self.url = "https://example.com/a"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            yield self._body

    body = b"<html><body>" + b"a readable sentence here. " * 20 + b"</body></html>"

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **k):
            Client.asked.append(url)
            return Resp(body)

        asked = []

    old_client, old_guard = fetch.httpx.AsyncClient, fetch.is_public_url
    fetch.httpx.AsyncClient = Client
    fetch.is_public_url = lambda u: "127.0.0.1" not in u
    fetch._cache.clear()
    try:
        first = asyncio.run(fetch.read({"url": "https://example.com/a"}))
        second = asyncio.run(fetch.read({"url": "https://example.com/a"}))
        assert first[0] and second[0], (first, second)
        assert second[1] == first[1], "the second read did not return the cached text"
        assert len(Client.asked) == 1, Client.asked

        # A failure is not cached: the site may be down for a moment, and the
        # next attempt has to go back to the network to find out.
        ok, _, reason = asyncio.run(fetch.read({"url": "http://127.0.0.1:8000/"}))
        assert not ok and reason == "not a public http(s) address", reason
        assert not [u for u, _ in fetch._cache if "127.0.0.1" in u], fetch._cache
    finally:
        fetch.httpx.AsyncClient, fetch.is_public_url = old_client, old_guard
        fetch._cache.clear()


def check_a_file_is_read_through_the_tool():
    """A long attachment is named in the prompt, not pasted into it, and its
    text comes back a slice at a time through read_file."""
    from app.tools import files
    from app.tools.registry import ToolContext

    text = "".join(f"line {i}\n" for i in range(2000))
    ctx = ToolContext(user_id=1, files=({"name": "notes.txt", "text": text},))

    ok, body, reason = files.read({"name": "notes.txt"}, ctx)
    assert ok and reason == "", (ok, reason)
    assert "notes.txt" in body and "line 0" in body, body[:200]
    assert f"of {len(text)}" in body, body[:200]
    assert "offset=" in body, "a cut read does not say where to continue"

    # The continuation picks up exactly where the first slice stopped.
    end = int(body.split("offset=")[1].split("]")[0])
    ok, more, _ = files.read({"name": "notes.txt", "offset": end}, ctx)
    assert ok and more.startswith(f"File: notes.txt (characters {end}-"), more[:80]

    # A model that drops the extension or the case still finds its file.
    assert files.read({"name": "NOTES"}, ctx)[0], "a looser name match did not find the file"
    assert files.read({"name": "other.txt"}, ctx)[2] == "no such attachment"
    assert files.read({}, ctx)[2] == "no file was named"
    # With nothing attached there is nothing to read, and the tool says so.
    assert files.read({"name": "x"}, ToolContext())[2] == "no file is attached"
    assert files.read({"name": "x", "offset": 5}, ctx)[2] == "no such attachment"
    assert files.read({"name": "notes.txt", "offset": len(text)}, ctx)[2] == "offset past the end"


def check_a_long_file_is_named_not_inlined():
    long_text = "x" * (api.INLINE_FILE_CHARS + 1)
    out = api._with_files("what is this?", [{"name": "big.md", "text": long_text}])
    assert "### File: big.md" in out and "read_file" in out, out[:300]
    assert long_text not in out, "the whole file was inlined anyway"
    assert out.endswith("what is this?"), out[-40:]

    short = api._with_files("q", [{"name": "s.txt", "text": "hello"}])
    assert "```txt\nhello\n```" in short, short


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


def check_weather_place_names():
    """The geocoder knows a city, not a city with its administrative prefix on
    it, and a small model prefixes it anyway."""
    from app.tools import weather

    assert weather._candidates("Bandung")[0] == "Bandung"
    loose = [c.lower() for c in weather._candidates("Kota Bandung")]
    assert "bandung" in loose, loose
    assert "bandung" in [c.lower() for c in weather._candidates("bandung city")]
    assert "jakarta" in [c.lower() for c in weather._candidates("Jakarta, Indonesia")]
    assert weather._candidates("   ") == []
    assert len(weather._candidates("a b c d e f")) <= weather.MAX_TRIES


def _weather_client(places, forecast):
    from app.tools import weather

    class Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            Client.asked.append((url, dict(params or {})))
            if url == weather.GEOCODE_URL:
                found = places.get(params["name"])
                return Resp({"results": [found]} if found else {})
            return Resp(forecast)

        asked = []

    return Client


def check_weather_reads_a_place():
    from app.tools import weather

    place = {"name": "Bandung", "admin1": "West Java", "country": "Indonesia",
             "latitude": -6.9, "longitude": 107.6}
    forecast = {
        "timezone": "Asia/Jakarta",
        "current": {"time": "2026-11-03T10:00", "temperature_2m": 27.4,
                    "apparent_temperature": 30.1, "relative_humidity_2m": 80,
                    "precipitation": 0.0, "weather_code": 3, "wind_speed_10m": 5.2},
        "daily": {"time": ["2026-11-03", "2026-11-04"],
                  "weather_code": [3, 61], "temperature_2m_max": [30.0, 29.0],
                  "temperature_2m_min": [22.0, 21.5], "precipitation_sum": [0.0, 4.2],
                  "precipitation_probability_max": [10, 70]},
    }
    old = weather.httpx.AsyncClient
    Client = _weather_client({"Bandung": place}, forecast)
    weather.httpx.AsyncClient = Client
    try:
        ok, text, reason, meta = asyncio.run(weather.read({"location": "Kota Bandung", "days": 2}))
    finally:
        weather.httpx.AsyncClient = old
    assert ok and reason == "", (ok, reason)
    assert "Bandung, West Java, Indonesia" in text, text
    assert "27.4°C" in text and "overcast" in text and "80.0%" in text, text
    assert "slight rain" in text and "70.0% chance" in text, text
    sources = meta["sources"]
    assert sources and sources[0]["kind"] == "web" and sources[0]["read"], sources
    # The card gets figures, not the sentences the model reads.
    card = meta["card"]
    assert card["place"] == "Bandung, West Java, Indonesia", card
    assert card["now"]["temp"] == 27.4 and card["now"]["text"] == "overcast", card["now"]
    assert [d["date"] for d in card["forecast"]] == ["2026-11-03", "2026-11-04"], card
    assert card["forecast"][1]["min"] == 21.5 and card["forecast"][1]["chance"] == 70.0, card
    # The forecast is asked for where the geocoder put the place, not where the
    # model named it.
    asked = [p for u, p in Client.asked if u == weather.FORECAST_URL]
    assert asked and asked[0]["latitude"] == -6.9 and asked[0]["longitude"] == 107.6, asked
    assert asked[0]["forecast_days"] == 2 and asked[0]["timezone"] == "auto", asked


def check_weather_says_when_it_does_not_know_the_place():
    from app.tools import weather

    old = weather.httpx.AsyncClient
    weather.httpx.AsyncClient = _weather_client({}, {})
    try:
        ok, text, reason, meta = asyncio.run(weather.read({"location": "Nowhereville"}))
        assert not ok and "Nowhereville" in text and meta == {}, (text, meta)
        assert reason, reason
        ok, text, reason, meta = asyncio.run(weather.read({}))
        assert not ok and text == "No place given.", text
        assert asyncio.run(weather.run({})) == "No place given."
    finally:
        weather.httpx.AsyncClient = old


def _crypto_client(answers):
    """A client whose reply per URL is given, or a `(status, payload)` pair."""
    from app.tools import crypto

    class Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise crypto.httpx.HTTPStatusError("bad", request=None, response=None)

        def json(self):
            return self._payload

    class Client:
        asked = []

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            Client.asked.append(url)
            status, payload = answers.get(url, (404, {}))
            return Resp(status, payload)

    return Client


def _rising_klines(n=120):
    """Binance-shaped daily rows, a straight climb, one dollar a bar."""
    rows = []
    for i in range(n):
        close = 100 + i
        rows.append([i, str(close - 1), str(close + 2), str(close - 3), str(close),
                     str(1000 + i)])
    return rows


def _flat_klines(n, price=100.0):
    """Candles that never move, so an indicator has a value that can be checked
    by hand rather than against a second implementation."""
    return [[i, str(price), str(price), str(price), str(price), "1000"] for i in range(n)]


def check_crypto_parses_what_the_model_sends():
    from app.tools import crypto

    assert crypto.parse_symbol("BTC") == ("BTC", "USD")
    assert crypto.parse_symbol("btc/usdt") == ("BTC", "USDT")
    assert crypto.parse_symbol("eth-usd") == ("ETH", "USD")
    assert crypto.parse_symbol("bitcoin") == ("BTC", "USD")
    assert crypto.parse_symbol("sol/eur") == ("SOL", "EUR")
    assert crypto.parse_symbol("") == (None, "USD")
    # A quote given as its own field still wins over a bare symbol.
    assert crypto.parse_quote({"symbol": "BTC", "quote": "idr"}) == ("BTC", "IDR")


def check_crypto_reads_the_first_source_that_answers():
    from app.tools import crypto

    old = crypto.httpx.AsyncClient
    crypto.httpx.AsyncClient = _crypto_client({crypto.BINANCE_URL: (200, {
        "symbol": "BTCUSDT", "lastPrice": "77334.01000000",
        "priceChangePercent": "-0.665", "highPrice": "78000", "lowPrice": "76000",
        "quoteVolume": "28112962441",
    })})
    try:
        ok, text, reason, meta = asyncio.run(crypto.read({"symbol": "btc"}))
    finally:
        crypto.httpx.AsyncClient = old
    assert ok and reason == "", (ok, reason, text)
    assert "77,334.01 USD" in text and "-0.67%" in text, text
    assert "24h high" in text and "28.11B" in text, text
    card = meta["card"]
    assert card["symbol"] == "BTC" and card["source"] == "Binance", card
    assert card["price"] == 77334.01 and card["change"] == -0.665, card
    assert meta["sources"][0]["source"] == crypto.BINANCE_URL, meta["sources"]


def check_crypto_rotates_when_a_source_is_rate_limited():
    """The whole point of asking three: one refusing must not end the call."""
    from app.tools import crypto

    old = crypto.httpx.AsyncClient
    Client = _crypto_client({
        crypto.BINANCE_URL: (429, {}),
        crypto.COINBASE_URL.format(pair="BTC-USD"): (200, {
            "open": "77396.56", "high": "77745.38", "low": "76238.54",
            "last": "77162.11", "volume": "7012.03",
        }),
    })
    crypto.httpx.AsyncClient = Client
    try:
        ok, text, reason, meta = asyncio.run(crypto.read({"symbol": "BTC"}))
    finally:
        crypto.httpx.AsyncClient = old
    assert ok and reason == "", (ok, reason, text)
    assert meta["card"]["source"] == "Coinbase", meta["card"]
    # Coinbase gives an open rather than a percentage, so the move is derived.
    assert meta["card"]["change"] == -0.3, meta["card"]["change"]
    # Its volume is in the base coin, so it is converted into the quote.
    assert round(meta["card"]["volume"]) == round(7012.03 * 77162.11), meta["card"]
    assert Client.asked == [crypto.BINANCE_URL,
                            crypto.COINBASE_URL.format(pair="BTC-USD")], Client.asked


def check_crypto_falls_through_to_the_last_source():
    from app.tools import crypto

    old = crypto.httpx.AsyncClient
    crypto.httpx.AsyncClient = _crypto_client({crypto.GECKO_URL: (200, [{
        "id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
        "current_price": 77323, "price_change_percentage_24h": -0.75,
        "high_24h": 77705, "low_24h": 76000, "total_volume": 28112962441,
        "market_cap": 1554681972439, "market_cap_rank": 1,
    }])})
    try:
        ok, text, reason, meta = asyncio.run(crypto.read({"symbol": "bitcoin"}))
    finally:
        crypto.httpx.AsyncClient = old
    assert ok, (reason, text)
    card = meta["card"]
    assert card["source"] == "CoinGecko" and card["name"] == "Bitcoin", card
    assert card["market_cap"] and card["rank"] == 1, card
    # A cap past a trillion takes the T tier, so the figure is one group, not
    # six digits of billions.
    assert "1.55T" in text and "1,554.68B" not in text, text


def check_a_trillion_is_stated_as_one_group():
    from app.tools import crypto

    assert crypto._big(1_217_000_000_000_000) == "1,217.00T"
    assert crypto._big(1_554_681_972_439) == "1.55T"
    # The tier below is untouched: a billion is still a billion.
    assert crypto._big(28_112_962_441) == "28.11B"
    assert crypto._big(78_000_000) == "78.00M"
    assert crypto._big(None) == "?"
    # Just under a tier rounds up at the two decimal places shown, which reads
    # as the next tier. Worth knowing, not worth a branch to hide.
    assert crypto._big(999_999_999_999) == "1,000.00B"


def check_market_indicators_on_numbers_that_can_be_checked():
    from app.tools import market

    # A flat series has no gains and no losses, which is the branch where a
    # naive RSI divides by zero.
    flat = [100.0] * 30
    assert market._sma(flat, 20) == 100.0, market._sma(flat, 20)
    assert market._rsi(flat) == 100.0, market._rsi(flat)
    assert market._slope(flat, 10) == 0.0, market._slope(flat, 10)

    # Not enough bars is None, not a number computed from whatever is there.
    assert market._sma(flat[:10], 20) is None
    assert market._rsi(flat[:10]) is None
    assert market._slope(flat[:10], 10) is None

    # A straight climb of one a day: the last 14 moves are all gains.
    climb = [float(100 + i) for i in range(30)]
    assert market._rsi(climb) == 100.0
    # Averages of 110–119 and 120–129: 114.5 to 124.5, so 8.73% higher.
    assert market._slope(climb, 10) == round((124.5 - 114.5) / 114.5 * 100, 2)
    assert market._change(climb, 10) == round((129 - 119) / 119 * 100, 2)


def check_market_reads_a_trend_from_a_rise():
    from app.tools import crypto, market

    old = crypto.httpx.AsyncClient
    crypto.httpx.AsyncClient = _crypto_client({
        market.KLINE_URL: (200, _rising_klines(120)),
    })
    try:
        ok, text, reason, meta = asyncio.run(market.read_trend({"symbol": "BTC"}))
    finally:
        crypto.httpx.AsyncClient = old
    assert ok and reason == "", (ok, reason, text)
    card = meta["card"]
    assert card["verdict"] in ("uptrend", "leaning up"), card["verdict"]
    assert card["source"] == "Binance" and card["bar_count"] == 120, card
    # The close is above both averages by construction, so both deltas are up.
    assert card["above20"] > 0 and card["above50"] > 0, card
    assert card["rsi"] == 100.0, card["rsi"]
    assert "not a forecast" in text, text
    assert meta["sources"][0]["source"] == market.KLINE_URL, meta["sources"]


def check_market_ships_the_candles_with_their_own_averages():
    from app.tools import crypto, market

    old = crypto.httpx.AsyncClient
    crypto.httpx.AsyncClient = _crypto_client({
        market.KLINE_URL: (200, _rising_klines(60)),
    })
    try:
        ok, text, reason, meta = asyncio.run(market.read_trend({"symbol": "BTC", "days": 60}))
    finally:
        crypto.httpx.AsyncClient = old
    assert ok and reason == "", (ok, reason, text)
    card = meta["card"]
    assert len(card["candles"]) == 60 and card["bar_count"] == 60, card["bar_count"]
    assert card["interval"] == "1d", card["interval"]
    # The averages are computed server-side and shipped, so the browser never
    # invents a second answer.
    assert len(card["ma20_series"]) == 60 and len(card["ma50_series"]) == 60, "ma series length"
    assert card["ma20_series"][:19] == [None] * 19, card["ma20_series"][:20]
    assert card["ma20_series"][19] is not None, card["ma20_series"][:20]
    # The last 20 closes are 140 to 159.
    assert round(card["ma20_series"][-1], 4) == round(sum(range(140, 160)) / 20, 4), card["ma20_series"][-1]
    assert card["chart_high"] == 161.0 and card["chart_low"] == 97.0, (card["chart_high"], card["chart_low"])
    # The chart and the verdict read the same bars, so the window cannot differ.
    assert card["candles"][-1][4] == card["price"], (card["candles"][-1], card["price"])


def check_market_rotates_to_coinbase_and_reports_short_history():
    from app.tools import crypto, market

    old = crypto.httpx.AsyncClient
    candles = [[100, "10", "12", "9", "11", "5"] for _ in range(40)]
    Client = _crypto_client({
        market.KLINE_URL: (429, {}),
        market.CANDLE_URL.format(pair="BTC-USD"): (200, candles),
    })
    crypto.httpx.AsyncClient = Client
    try:
        ok, text, reason, meta = asyncio.run(market.read_trend({"symbol": "BTC"}))
    finally:
        crypto.httpx.AsyncClient = old
    assert ok, (reason, text)
    assert meta["card"]["source"] == "Coinbase", meta["card"]
    # Coinbase sends newest first and as [time, low, high, open, close, volume];
    # both are corrected before anything reads the bar.
    assert meta["card"]["price"] == 11.0, meta["card"]["price"]
    assert Client.asked[0] == market.KLINE_URL, Client.asked

    old = crypto.httpx.AsyncClient
    crypto.httpx.AsyncClient = _crypto_client({
        market.KLINE_URL: (200, _rising_klines(8)),
        market.CANDLE_URL.format(pair="BTC-USD"): (200, []),
    })
    try:
        ok, text, reason, meta = asyncio.run(market.read_trend({"symbol": "BTC"}))
    finally:
        crypto.httpx.AsyncClient = old
    assert not ok and meta == {}, (ok, meta)
    assert "too few" in text, text


def check_market_says_when_no_candle_source_answers():
    from app.tools import crypto, market

    old = crypto.httpx.AsyncClient
    try:
        crypto.httpx.AsyncClient = _crypto_client({
            market.KLINE_URL: (400, {}),
            market.CANDLE_URL.format(pair="ZZZZ-USD"): (404, {}),
        })
        ok, text, reason, meta = asyncio.run(market.read_trend({"symbol": "ZZZZ"}))
        assert not ok and "No candle source lists" in text, text

        crypto.httpx.AsyncClient = _crypto_client({
            market.KLINE_URL: (429, {}),
            market.CANDLE_URL.format(pair="BTC-USD"): (429, {}),
        })
        ok, text, reason, meta = asyncio.run(market.read_trend({"symbol": "BTC"}))
        assert not ok and "rate-limiting" in text, text

        ok, text, reason, meta = asyncio.run(market.read_trend({}))
        assert not ok and text == "No coin given.", text
    finally:
        crypto.httpx.AsyncClient = old


def check_crypto_says_when_no_source_lists_the_coin():
    from app.tools import crypto

    old = crypto.httpx.AsyncClient
    crypto.httpx.AsyncClient = _crypto_client({})
    try:
        ok, text, reason, meta = asyncio.run(crypto.read({"symbol": "ZZZZ"}))
        assert not ok and meta == {}, (ok, meta)
        assert "ZZZZ" in text, text
        assert "rate-limiting" not in text, text
        # Every source refusing with a limit is a wait, not a bad ticker.
        crypto.httpx.AsyncClient = _crypto_client({
            crypto.BINANCE_URL: (429, {}),
            crypto.COINBASE_URL.format(pair="BTC-USD"): (429, {}),
            crypto.GECKO_URL: (429, {}),
        })
        ok, text, reason, meta = asyncio.run(crypto.read({"symbol": "BTC"}))
        assert not ok and "rate-limiting" in text, text
        # No coin at all is refused before anything is asked.
        ok, text, reason, meta = asyncio.run(crypto.read({}))
        assert not ok and text == "No coin given.", text
    finally:
        crypto.httpx.AsyncClient = old


def check_wayback_finds_and_reads_a_snapshot():
    from app.tools import wayback

    rows = [["timestamp", "original", "statuscode", "mimetype"],
            ["20190401120000", "https://example.com/gone", "200", "text/html"],
            ["20180101120000", "https://example.com/gone", "200", "text/html"]]

    class Resp:
        status_code = 200
        text = json.dumps(rows)

        def raise_for_status(self):
            pass

        def json(self):
            return rows

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            Client.asked.append((url, params))
            return Resp()

        asked = []

    async def fake_fetch_page(client, url, max_chars):
        fake_fetch_page.asked.append(url)
        return "the page as it was", ""

    fake_fetch_page.asked = []
    old_client = wayback.httpx.AsyncClient
    old_public, old_page = wayback.is_public_url, wayback.fetch_page
    wayback.httpx.AsyncClient = Client
    wayback.is_public_url = lambda u: True
    wayback.fetch_page = fake_fetch_page
    try:
        ok, text, reason, meta = asyncio.run(wayback.read({"url": "example.com/gone"}))
    finally:
        wayback.httpx.AsyncClient = old_client
        wayback.is_public_url = old_public
        wayback.fetch_page = old_page
    assert ok and reason == "", (ok, reason)
    assert "2019-04-01" in text and "the page as it was" in text, text
    assert "2018-01-01" in text, text
    sources = meta["sources"]
    assert sources[0]["source"] == "https://web.archive.org/web/20190401120000id_/https://example.com/gone"
    assert sources[0]["kind"] == "web"
    assert fake_fetch_page.asked == [sources[0]["source"]], fake_fetch_page.asked
    # The card shows the snapshot that was read, and lists the ones it was not.
    card = meta["card"]
    assert card["when"] == "2019-04-01" and card["chars"] == len("the page as it was"), card
    assert card["url"] == "https://example.com/gone", card
    assert card["read"] == sources[0]["source"], card
    assert card["older"] == [
        {"when": "2018-01-01", "url": "https://example.com/gone", "type": "text/html",
         "read": "https://web.archive.org/web/20180101120000id_/https://example.com/gone"}
    ], card


def check_wayback_windows_a_partial_date():
    """No timestamp parameter on the index: a bare year there answers with rows
    from years the caller never asked about."""
    from app.tools import wayback

    assert wayback._window("2019") == ("20190101", "20191231")
    assert wayback._window("2019-04") == ("20190401", "20190430")
    assert wayback._window("2019-04-01") == ("20190401", "20190401")
    assert wayback._window("last tuesday") is None
    assert wayback._window("") is None


def check_wayback_says_when_nothing_is_archived():
    from app.tools import wayback

    class Resp:
        status_code = 200
        text = "[]"

        def raise_for_status(self):
            pass

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            return Resp()

    old_client = wayback.httpx.AsyncClient
    old_public = wayback.is_public_url
    wayback.httpx.AsyncClient = Client
    wayback.is_public_url = lambda u: True
    try:
        ok, text, reason, meta = asyncio.run(wayback.read({"url": "https://example.com/never"}))
        assert not ok and "no snapshot" in text and meta == {}, text
        assert reason, reason
        ok, text, reason, meta = asyncio.run(wayback.read({}))
        assert not ok and text == "No URL given.", text
        ok, text, reason, meta = asyncio.run(wayback.read({"url": "http://127.0.0.1/x"}))
        assert not ok and meta == {}, text
    finally:
        wayback.httpx.AsyncClient = old_client
        wayback.is_public_url = old_public


def check_wayback_refuses_a_private_address():
    from app.tools import wayback

    ok, text, reason, meta = asyncio.run(wayback.read({"url": "http://169.254.169.254/latest/meta-data/"}))
    assert not ok and reason == "not a public http(s) address", reason
    assert "169.254" in text, text


def check_registry_chat_tools():
    from app.tools.registry import registry
    chat_tools = {t.name for t in registry.all() if "chat" in t.tags}
    assert chat_tools == {"web_search", "fetch_url", "read_file", "weather", "wayback",
                          "crypto", "crypto_trend", "equity", "equity_trend", "screen"}


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


def check_cards_roundtrip():
    import os
    import tempfile
    from app import chats

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["CHAT_DB"] = path
    chats._db = None

    cards = [
        {"r": 0, "tool": "weather", "card": {"place": "Bandung", "now": {"temp": 27.4}}},
        {"r": 1, "tool": "wayback", "card": None, "reason": "the archive could not be reached"},
    ]
    chats.save_turn(1, "c1", "assistant", "lihat ini", cards=cards)
    assert chats.turns(1, "c1")[0]["cards"] == cards

    # A failed card keeps its reason without a payload, and a turn with no cards
    # stores no field at all rather than an empty list.
    chats.save_turn(1, "c1", "assistant", "tanpa kartu")
    assert "cards" not in chats.turns(1, "c1")[-1]

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


def _equity_client(answers, crumb="TESTCRUMB"):
    """Yahoo's endpoints without a network. `answers` maps a URL to
    `(status, payload)`, or to a list of those consumed one per call so a 401
    followed by a retry can be modelled."""
    from app.tools import equity

    class Resp:
        def __init__(self, status, payload, text=""):
            self.status_code = status
            self._payload = payload
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise equity.httpx.HTTPStatusError("bad", request=None, response=None)

        def json(self):
            return self._payload

    class Client:
        asked = []
        posts = []
        seen = {}

        def __init__(self, *a, **k):
            self.cookies = equity.httpx.Cookies()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return False

        def _reply(self, url):
            entry = answers.get(url, (404, {}))
            if isinstance(entry, list):
                i = Client.seen.get(url, 0)
                Client.seen[url] = i + 1
                entry = entry[min(i, len(entry) - 1)]
            return Resp(entry[0], entry[1])

        async def get(self, url, params=None, **k):
            Client.asked.append((url, dict(params or {})))
            if url == equity.CRUMB_URL:
                return Resp(200, None, text=crumb)
            return self._reply(url)

        async def post(self, url, params=None, json=None, **k):
            Client.posts.append((url, dict(params or {}), json))
            return self._reply(url)

    return Client

def _yahoo_chart(n=60, gap=None):
    """A chart payload shaped the way Yahoo sends one. `gap` is an index whose
    close arrives null, which is a bar that never traded."""
    stamps = [1_700_000_000 + i * 86_400 for i in range(n)]
    def seq(f):
        return [None if i == gap else f(100 + i) for i in range(n)]
    return {"chart": {"error": None, "result": [{
        "meta": {"currency": "IDR", "shortName": "Bank Central Asia",
                 "fullExchangeName": "Jakarta"},
        "timestamp": stamps,
        "indicators": {"quote": [{
            "open": seq(lambda c: c - 1), "high": seq(lambda c: c + 2),
            "low": seq(lambda c: c - 3), "close": seq(float),
            "volume": seq(lambda c: 1000 + c),
        }]},
    }]}}

def check_equity_reads_a_quote_with_the_cookie_it_had_to_fetch():
    """The quote endpoint wants a crumb, so a call that never had one has to
    ask for it first, and the figure that comes back has to land on the card."""
    from app.tools import equity

    old_client, old_cache = equity.httpx.AsyncClient, dict(equity._cache)
    equity._cache.update({"crumb": "", "cookies": {}})
    Client = _equity_client({equity.QUOTE_URL: (200, {"quoteResponse": {"result": [{
        "symbol": "BBCA.JK", "shortName": "Bank Central Asia Tbk",
        "currency": "IDR", "regularMarketPrice": 9875.0,
        "regularMarketChangePercent": 1.28, "regularMarketDayHigh": 9950.0,
        "regularMarketDayLow": 9700.0, "regularMarketPreviousClose": 9750.0,
        "regularMarketVolume": 78000000, "marketCap": 1217000000000000,
        "trailingPE": 21.4, "dividendYield": 2.6,
        "fiftyTwoWeekHigh": 10200.0, "fiftyTwoWeekLow": 8100.0,
        "fullExchangeName": "Jakarta", "quoteType": "EQUITY",
        "marketState": "CLOSE",
    }]}})})
    equity.httpx.AsyncClient = Client
    try:
        ok, text, reason, meta = asyncio.run(equity.read({"symbol": "BBCA.JK"}))
    finally:
        equity.httpx.AsyncClient = old_client
        equity._cache.update(old_cache)

    assert ok and reason == "", (ok, reason, text)
    card = meta["card"]
    assert card["quotes"][0]["symbol"] == "BBCA.JK", card
    assert card["quotes"][0]["price"] == 9875.0, card
    assert card["quotes"][0]["change"] == 1.28, card
    assert card["missing"] == [], card
    assert "9,875.00 IDR" in text and "+1.28% today" in text, text
    # The crumb was fetched before the guarded call, and rode on it.
    urls = [u for u, _ in Client.asked]
    assert equity.CRUMB_URL in urls, urls
    guarded = [p for u, p in Client.asked if u == equity.QUOTE_URL]
    assert guarded and guarded[0].get("crumb") == "TESTCRUMB", guarded

def check_equity_resolves_a_name_through_the_search():
    from app.tools import equity

    old_client, old_cache = equity.httpx.AsyncClient, dict(equity._cache)
    equity._cache.update({"crumb": "c", "cookies": {}})
    Client = _equity_client({
        equity.SEARCH_URL: (200, {"quotes": [
            {"symbol": "BMRI.JK", "quoteType": "EQUITY"},
            {"symbol": "BMRI", "quoteType": "MUTUALFUND"},
        ]}),
        equity.QUOTE_URL: (200, {"quoteResponse": {"result": [{
            "symbol": "BMRI.JK", "shortName": "Bank Mandiri", "currency": "IDR",
            "regularMarketPrice": 6100.0, "regularMarketChangePercent": -0.81,
        }]}}),
    })
    equity.httpx.AsyncClient = Client
    try:
        ok, text, reason, meta = asyncio.run(equity.read({"symbol": "bank mandiri"}))
    finally:
        equity.httpx.AsyncClient = old_client
        equity._cache.update(old_cache)

    assert ok and reason == "", (ok, reason, text)
    # The equity listing wins over the fund that shares the ticker.
    assert meta["card"]["quotes"][0]["symbol"] == "BMRI.JK", meta["card"]
    assert equity.SEARCH_URL in [u for u, _ in Client.asked], Client.asked

def check_equity_says_which_symbol_it_could_not_find():
    from app.tools import equity

    old_client, old_cache = equity.httpx.AsyncClient, dict(equity._cache)
    equity._cache.update({"crumb": "c", "cookies": {}})
    Client = _equity_client({
        equity.QUOTE_URL: (200, {"quoteResponse": {"result": [{
            "symbol": "AAPL", "shortName": "Apple Inc.", "currency": "USD",
            "regularMarketPrice": 191.2, "regularMarketChangePercent": 0.4,
        }]}}),
    })
    equity.httpx.AsyncClient = Client
    try:
        ok, text, reason, meta = asyncio.run(equity.read({"symbol": "AAPL,ZZZZ"}))
    finally:
        equity.httpx.AsyncClient = old_client
        equity._cache.update(old_cache)

    # One symbol the endpoint never answered for is named rather than dropped,
    # because a two-name call that returns one row leaves the reader guessing.
    assert ok and meta["card"]["missing"] == ["ZZZZ"], meta["card"]
    assert "No listing was found for: ZZZZ" in text, text

def check_equity_quote_tool_states_its_own_limits():
    from app.tools import equity

    # A market with no screen, and a sort Yahoo does not take, are both answered
    # before any request goes out.
    ok, text, reason, meta = asyncio.run(equity.read_screen({"region": "atlantis"}))
    assert not ok and reason == "no screen for that market", (reason, text)
    assert equity._region("Indonesia") == "id"
    assert equity._region("ID") == "id"
    assert equity._region("Atlantis") == ""
    # Nothing was given, so the default market applies rather than an error.
    assert equity._region("") == "id"

    ok, text, reason, meta = asyncio.run(
        equity.read_screen({"region": "id", "sort": "cheapest"}))
    assert not ok and reason == "unsupported sort", (reason, text)

    ok, text, reason, meta = asyncio.run(equity.read({"symbol": "  "}))
    assert not ok and reason == "no symbol was given", (reason, text)

def check_equity_trend_drops_a_bar_that_never_traded():
    """Yahoo leaves the session in progress in the arrays as nulls. A null
    close is carried through as a bar and every average built on it is wrong,
    so it is dropped before the arithmetic runs."""
    from app.tools import equity

    old_client, old_cache = equity.httpx.AsyncClient, dict(equity._cache)
    equity._cache.update({"crumb": "c", "cookies": {}})
    url = equity.CHART_URL.format(symbol="BBCA.JK")
    Client = _equity_client({url: (200, _yahoo_chart(60, gap=59))})
    equity.httpx.AsyncClient = Client
    try:
        ok, text, reason, meta = asyncio.run(
            equity.read_trend({"symbol": "BBCA.JK", "days": 180}))
    finally:
        equity.httpx.AsyncClient = old_client
        equity._cache.update(old_cache)

    assert ok and reason == "", (ok, reason, text)
    card = meta["card"]
    assert card["bar_count"] == 59, card["bar_count"]
    assert len(card["candles"]) == 59, len(card["candles"])
    assert all(isinstance(b[4], float) for b in card["candles"]), "a null close survived"
    assert card["interval"] == "1d", card
    assert card["name"] == "Bank Central Asia" and card["quote"] == "IDR", card
    # The chart and the verdict read the same bars, so the window cannot differ.
    assert card["candles"][-1][4] == card["price"], (card["candles"][-1], card["price"])
    assert card["verdict"] in ("uptrend", "leaning up"), card["verdict"]
    # It is a reading of the past and the text says so.
    assert "not a forecast" in text, text

def check_equity_trend_rebuilds_a_session_the_crumb_aged_out_of():
    """A 401 means the crumb went stale, so the session is rebuilt once and the
    call retried. Without it the reading would fail on a cookie's lifetime."""
    from app.tools import equity

    old_client, old_cache = equity.httpx.AsyncClient, dict(equity._cache)
    equity._cache.update({"crumb": "stale", "cookies": {}})
    Client = _equity_client({equity.QUOTE_URL: [
        (401, {}),
        (200, {"quoteResponse": {"result": [{
            "symbol": "AAPL", "shortName": "Apple Inc.", "currency": "USD",
            "regularMarketPrice": 191.2, "regularMarketChangePercent": 0.4,
        }]}}),
    ]})
    equity.httpx.AsyncClient = Client
    try:
        ok, text, reason, meta = asyncio.run(equity.read({"symbol": "AAPL"}))
    finally:
        equity.httpx.AsyncClient = old_client
        equity._cache.update(old_cache)

    assert ok and reason == "", (ok, reason, text)
    assert meta["card"]["quotes"][0]["symbol"] == "AAPL", meta["card"]
    assert [u for u, _ in Client.asked].count(equity.QUOTE_URL) == 2, Client.asked
    assert equity.CRUMB_URL in [u for u, _ in Client.asked], Client.asked

def check_equity_screen_ranks_one_market():
    from app.tools import equity

    old_client, old_cache = equity.httpx.AsyncClient, dict(equity._cache)
    equity._cache.update({"crumb": "c", "cookies": {}})
    Client = _equity_client({equity.SCREEN_URL: (200, {"finance": {"result": [{
        "total": 839, "count": 2, "quotes": [
            {"symbol": "BBCA.JK", "shortName": "Bank Central Asia",
             "currency": "IDR", "regularMarketPrice": 9875.0,
             "regularMarketChangePercent": 1.28, "regularMarketVolume": 78000000},
            {"symbol": "GOTO.JK", "shortName": "GoTo Gojek Tokopedia",
             "currency": "IDR", "regularMarketPrice": 62.0,
             "regularMarketChangePercent": 3.33, "regularMarketVolume": 1200000000},
            # A row with no price is dropped: it would read as a zero.
            {"symbol": "HALT.JK", "shortName": "Halted", "currency": "IDR"},
        ],
    }]}})})
    equity.httpx.AsyncClient = Client
    try:
        ok, text, reason, meta = asyncio.run(
            equity.read_screen({"region": "indonesia", "sort": "gainers", "limit": 25}))
    finally:
        equity.httpx.AsyncClient = old_client
        equity._cache.update(old_cache)

    assert ok and reason == "", (ok, reason, text)
    card = meta["card"]
    assert card["market"] == "Indonesia" and card["region"] == "id", card
    assert card["total"] == 839 and len(card["rows"]) == 2, card
    assert "HALT.JK" not in [r["symbol"] for r in card["rows"]], card["rows"]
    assert "Biggest gainers on Indonesia" in text, text
    # The sort the user asked for is the one that goes out, not a default.
    _, params, body = Client.posts[0]
    assert body["sortField"] == "percentchange" and body["sortType"] == "DESC", body
    assert body["size"] == 25 and body["query"]["operands"][0]["operands"] == ["region", "id"], body
    assert params.get("crumb") == "c", params

def check_equity_intraday_change_the_yahoo_shape_carries():
    """The row normaliser is the one place the field names are spelled out, so
    it is checked against a payload with every key it reads."""
    from app.tools import equity

    row = equity._row({
        "symbol": "7203.T", "longName": "Toyota Motor Corporation", "currency": "JPY",
        "regularMarketPrice": 2710.5, "regularMarketChangePercent": -0.42,
        "regularMarketDayHigh": 2733.0, "regularMarketDayLow": 2690.0,
        "regularMarketPreviousClose": 2722.0, "regularMarketVolume": 24500000,
        "marketCap": 44000000000000, "trailingPE": 9.8, "dividendYield": 3.1,
        "fiftyTwoWeekHigh": 3200.0, "fiftyTwoWeekLow": 2100.0,
        "exchange": "JPX", "quoteType": "EQUITY", "marketState": "POST",
    })
    assert row["name"] == "Toyota Motor Corporation", row
    assert row["quote"] == "JPY" and row["price"] == 2710.5, row
    assert row["exchange"] == "JPX" and row["kind"] == "EQUITY", row
    assert row["url"] == "https://finance.yahoo.com/quote/7203.T", row

    # A halted listing reports a zero volume, which is not a volume; the same
    # for a cap of nothing. Both are dropped rather than printed as facts.
    halted = equity._row({"symbol": "X", "regularMarketPrice": 1.0,
                          "regularMarketVolume": 0, "marketCap": 0})
    assert halted["volume"] is None and halted["market_cap"] is None, halted

for name, fn in sorted(globals().items()):
    if name.startswith("check_"):
        fn()
        print(f"ok  {name}")
print("all checks passed")
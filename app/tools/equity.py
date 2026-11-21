"""Equities: quotes, daily candles and screeners, from Yahoo Finance.

Yahoo documents none of this. The quotes and the screener are guarded by a
cookie and a crumb, so that pair is fetched once, kept, and fetched again when
a call comes back 401. The chart endpoint needs neither, which is why the trend
reading still answers when the handshake itself is broken. Nothing here places
an order: every request reads a public page of market data.
"""

import asyncio
import re
import time

import httpx
from contextlib import asynccontextmanager

from ..errors import TOOL_ERROR
from ..net import HEADERS
from ..security import EXTERNAL
from .marketdata import (
    MIN_BARS, Unavailable, analyse, big, money, num, pos, trend_card,
    trend_prose, when,
)
from .registry import TAGS, Tool, ToolResult, days_arg, missing_arg, registry, source_row

CONSENT_URL = "https://fc.yahoo.com"
CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"
CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
QUOTE_URL = "https://query2.finance.yahoo.com/v7/finance/quote"
SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
SCREEN_URL = "https://query2.finance.yahoo.com/v1/finance/screener"
PAGE_URL = "https://finance.yahoo.com/quote/{symbol}"

TIMEOUT = 12
MAX_CHARS = 3_000
# Yahoo answered 144 of a 150-symbol quote request and none of a 200-symbol
# one, so a batch stays well under that ceiling rather than probing it.
BATCH = 100
MAX_SYMBOLS = 10
MIN_DAYS = 30
MAX_DAYS = 1_825
DEFAULT_DAYS = 180
# Three tools live in this module, so each carries its own name and timeout the
# way a single-tool module does.
NAME = "equity"
TIMEOUT_S = 35.0
TREND_NAME = "equity_trend"
TREND_TIMEOUT_S = 40.0
SCREEN_NAME = "screen"
SCREEN_TIMEOUT_S = 35.0
# What a screen may ask for, and what it gets when it asks for nothing. The
# schema states these and `read_screen` clamps to them, so they are one pair.
SCREEN_MIN = 5
SCREEN_MAX = 50
SCREEN_DEFAULT = 10


# Yahoo's own search returns nothing for these, but they are the two the
# question is most likely to name outright, so they are mapped rather than
# left to a search that answers with silence.
ALIASES = {
    "ihsg": "^JKSE",
    "idx": "^JKSE",
    "idx composite": "^JKSE",
    "jakarta composite": "^JKSE",
    "lq45": "^JKLQ45",
    "idx lq45": "^JKLQ45",
    "jakarta islamic": "^JKII",
}

# The one-word names a question uses for an exchange index, derived from the
# table rather than restated: every other key is a phrase, which a caller reads
# out of the table instead of being told it in a schema description.
ALIAS_NAMES = tuple(key.upper() for key in ALIASES if " " not in key)

REGIONS = {
    "id": "Indonesia",
    "us": "United States",
    "jp": "Japan",
    "hk": "Hong Kong",
    "gb": "United Kingdom",
    "de": "Germany",
    "in": "India",
    "br": "Brazil",
    "ca": "Canada",
    "au": "Australia",
    "sg": "Singapore",
    "kr": "South Korea",
    "tw": "Taiwan",
    "my": "Malaysia",
    "th": "Thailand",
}

REGION_ALIASES = {
    "indonesia": "id", "idx": "id", "jakarta": "id",
    "usa": "us", "united states": "us", "america": "us",
    "japan": "jp", "tokyo": "jp",
    "hong kong": "hk",
    "uk": "gb", "united kingdom": "gb", "london": "gb",
    "germany": "de", "frankfurt": "de",
    "india": "in", "mumbai": "in",
    "brazil": "br", "canada": "ca", "australia": "au", "singapore": "sg",
    "korea": "kr", "south korea": "kr", "taiwan": "tw",
    "malaysia": "my", "thailand": "th",
}

# The sort field Yahoo accepts, and which way. `intradaymarketcap` is the one
# that filters as a number as well as sorting; `marketcap` only sorts.
SORTS = {
    "gainers": ("percentchange", "DESC", "biggest gainers"),
    "losers": ("percentchange", "ASC", "biggest losers"),
    "active": ("dayvolume", "DESC", "most traded"),
    "largest": ("intradaymarketcap", "DESC", "largest by market cap"),
}

# `^` and `.` are the two things a ticker can carry and a search string almost
# never does. Anything else is a name and goes through the search.
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")

_cache = {"crumb": "", "cookies": {}}


async def _handshake(client):
    """The cookie and crumb pair the guarded endpoints want. `fc.yahoo.com`
    answers 404, which is the expected reply: what is wanted is the cookie the
    redirect leaves behind."""
    await client.get(CONSENT_URL)
    r = await client.get(CRUMB_URL)
    if r.status_code != 200:
        return False
    crumb = r.text.strip()
    if not crumb:
        return False
    _cache["crumb"] = crumb
    _cache["cookies"] = {k: v for k, v in client.cookies.items()}
    return True


@asynccontextmanager
async def _client():
    """An entered client holding the cached cookie, or a fresh handshake.

    The handshake runs inside the `async with`, not before it: httpx 0.28
    refuses to enter a client that has already sent a request, so a handshake
    on an unopened client would make every first call fail on the enter that
    followed it.
    """
    client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers=HEADERS)
    try:
        for name, value in _cache["cookies"].items():
            client.cookies.set(name, value, domain=".yahoo.com")
        async with client:
            if not _cache["crumb"] and not await _handshake(client):
                raise Unavailable("yahoo refused the session", unreached=True)
            yield client
    finally:
        await client.aclose()


async def _json(client, url, params=None, guarded=False, method="GET", body=None):
    """One call. A 401 means the crumb aged out, so the session is rebuilt once
    and the call retried; the second 401 is reported rather than looped on."""
    for attempt in (0, 1):
        query = dict(params or {})
        if guarded and _cache["crumb"]:
            query["crumb"] = _cache["crumb"]
        if method == "POST":
            r = await client.post(url, params=query, json=body)
        else:
            r = await client.get(url, params=query)
        if guarded and r.status_code == 401 and attempt == 0:
            _cache["crumb"] = ""
            if await _handshake(client):
                continue
        if r.status_code in (429, 999):
            raise Unavailable("yahoo is rate-limiting", limited=True)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            # A 200 whose body is not JSON is a service that answered with
            # something else, which is the same kind of failure as a refusal
            # and is reported as one rather than as a decoder message.
            raise Unavailable("yahoo sent a reply that could not be read")
    raise Unavailable("yahoo refused the session", unreached=True)


async def _search(client, text):
    return await _json(client, SEARCH_URL, {"q": text, "quotesCount": 8, "newsCount": 0})


async def _resolve(client, raw):
    """Yahoo's symbol for what the model sent, or `""`.

    A ticker is taken as sent. A name goes through Yahoo's search, which ranks
    the listing the name is best known by first; the exchange suffix is left on
    so an ambiguous answer can be corrected on the next call.
    """
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    alias = ALIASES.get(text.lower())
    if alias:
        return alias
    upper = text.upper()
    if TICKER_RE.match(upper):
        return upper
    d = await _search(client, text)
    quotes = d.get("quotes") or []
    for kind in ("EQUITY", "INDEX", "ETF"):
        for q in quotes:
            if q.get("quoteType") == kind and q.get("symbol"):
                return q["symbol"]
    return ""


async def _quote_rows(client, symbols):
    rows = []
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        d = await _json(client, QUOTE_URL, {"symbols": ",".join(chunk)}, guarded=True)
        rows.extend((d.get("quoteResponse") or {}).get("result") or [])
    return rows


def _row(q):
    symbol = q.get("symbol") or ""
    return {
        "symbol": symbol,
        "name": q.get("shortName") or q.get("longName") or symbol,
        "quote": q.get("currency"),
        "price": num(q.get("regularMarketPrice")),
        "change": num(q.get("regularMarketChangePercent")),
        "high": pos(num(q.get("regularMarketDayHigh"))),
        "low": pos(num(q.get("regularMarketDayLow"))),
        "prev": pos(num(q.get("regularMarketPreviousClose"))),
        "volume": pos(num(q.get("regularMarketVolume"))),
        "market_cap": pos(num(q.get("marketCap"))),
        "pe": num(q.get("trailingPE")),
        "dividend": num(q.get("dividendYield")),
        "week52_high": pos(num(q.get("fiftyTwoWeekHigh"))),
        "week52_low": pos(num(q.get("fiftyTwoWeekLow"))),
        "exchange": q.get("fullExchangeName") or q.get("exchange"),
        "kind": q.get("quoteType"),
        "market_state": q.get("marketState"),
        "url": PAGE_URL.format(symbol=symbol),
    }


def _render_rows(rows):
    lines = []
    for r in rows:
        line = f"{r['name']} ({r['symbol']}) on {r['exchange']}: "
        line += money(r["price"]) + (f" {r['quote']}" if r["quote"] else "")
        if r["change"] is not None:
            line += f", {r['change']:+.2f}% today"
        line += "."
        facts = []
        if r["prev"] is not None:
            facts.append(f"previous close {money(r['prev'])}")
        if r["high"] is not None and r["low"] is not None:
            facts.append(f"day range {money(r['low'])} to {money(r['high'])}")
        if r["week52_low"] is not None and r["week52_high"] is not None:
            facts.append(f"52-week range {money(r['week52_low'])} to "
                         f"{money(r['week52_high'])}")
        if r["volume"] is not None:
            facts.append(f"volume {big(r['volume'])}")
        if r["market_cap"] is not None:
            facts.append(f"market cap {big(r['market_cap'])} {r['quote'] or ''}".strip())
        if r["pe"] is not None:
            facts.append(f"P/E {r['pe']:.2f}")
        if r["dividend"] is not None:
            facts.append(f"dividend yield {r['dividend']:.2f}%")
        if r["market_state"]:
            facts.append(f"market state {r['market_state']}")
        if facts:
            line += " " + ", ".join(facts) + "."
        lines.append(line)
    lines.append(
        "Prices are Yahoo Finance's, delayed by up to 15 minutes on most "
        f"exchanges. Read at {when(int(time.time()))}. If a symbol looks "
        "like the wrong company, say which exchange you meant rather than "
        "guessing."
    )
    return "\n".join(lines)[:MAX_CHARS]


async def read(args):
    """Answer the quote tool, or say which failure stopped it."""
    raw = args.get("symbol") or ""
    parts = [p.strip() for p in re.split(r"[,;\n]+", str(raw)) if p.strip()]
    if not parts:
        return missing_arg("symbol")
    if len(parts) > MAX_SYMBOLS:
        return ToolResult.failed(
            TOOL_ERROR,
            f"{len(parts)} symbols were asked for; {MAX_SYMBOLS} is the most one "
            "call reads. Ask again for the rest.",
            reason="too many symbols in one call",
        )

    try:
        async with _client() as client:
            # Resolved together, not one after another: each name that is not a
            # bare ticker costs a search round trip, and ten of them in turn is
            # ten times the wait for no reason, since nothing here depends on
            # the order. The order of the answers is the order of the names, so
            # the sentence that lists what was not found still reads the way the
            # question was asked.
            found = await asyncio.gather(*(_resolve(client, p) for p in parts))
            symbols = []
            missing = []
            for part, symbol in zip(parts, found):
                (symbols if symbol else missing).append(symbol or part)
            if not symbols:
                text = (f"Nothing on Yahoo Finance matches {', '.join(parts)}. "
                        "Give a ticker with its exchange, such as BBCA.JK or 7203.T.")
                return ToolResult.failed(TOOL_ERROR, text, reason="no listing matches that name")

            rows = [_row(q) for q in await _quote_rows(client, symbols)]
            found = {r["symbol"] for r in rows}
            missing.extend(s for s in symbols if s not in found)
            rows = [r for r in rows if r["price"] is not None]
    except (Unavailable, httpx.HTTPError) as e:
        return _yahoo_error(e)

    if not rows:
        text = ("Yahoo Finance listed " + ", ".join(symbols) +
                " but sent no price for it, which usually means the listing is "
                "delisted or halted.")
        return ToolResult.failed(TOOL_ERROR, text, reason="no price came back")

    text = _render_rows(rows)
    if missing:
        text += "\nNo listing was found for: " + ", ".join(missing) + "."
    card = {
        "quotes": rows,
        "missing": missing,
        "source": "Yahoo Finance",
        "url": PAGE_URL.format(symbol=rows[0]["symbol"]),
        "at": int(time.time()),
    }
    row = source_row(rows[0]["url"], f"{rows[0]['symbol']} — Yahoo Finance")
    return ToolResult.ok(text, sources=[row], card=card)


async def _bars(client, symbol, day_span, interval):
    now = int(time.time())
    d = await _json(client, CHART_URL.format(symbol=symbol),
                    {"period1": now - day_span * 86_400, "period2": now,
                     "interval": interval})
    chart = d.get("chart") or {}
    if chart.get("error"):
        raise Unavailable("yahoo has no candles for that symbol")
    result = (chart.get("result") or [None])[0]
    if not result:
        raise Unavailable("yahoo has no candles for that symbol")

    # Yahoo leaves the session in progress in the arrays as nulls, and a null
    # close is a bar that never traded, so it is dropped rather than carried.
    quote = (result.get("indicators") or {}).get("quote") or [{}]
    quote = quote[0] if quote else {}
    stamps = result.get("timestamp") or []
    closes = quote.get("close") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    vols = quote.get("volume") or []

    def at(seq, i):
        return seq[i] if i < len(seq) else None

    bars = []
    for i, stamp in enumerate(stamps):
        close = at(closes, i)
        if close is None:
            continue
        bars.append({
            "t": int(stamp),
            "o": at(opens, i) or close,
            "h": at(highs, i) or close,
            "l": at(lows, i) or close,
            "c": float(close),
            "v": float(at(vols, i) or 0.0),
        })
    meta = result.get("meta") or {}
    return bars, meta


INTERVALS = {"1d": "daily", "1wk": "weekly", "1mo": "monthly"}
# Calendar days of prehistory for the 50-bar average in each interval.
TREND_PREHISTORY = {"1d": 100, "1wk": 360, "1mo": 1550}


async def read_trend(args):
    """Answer the equity trend tool, or say which failure stopped it."""
    raw = str(args.get("symbol") or "").strip()
    if not raw:
        return missing_arg("symbol")
    interval = str(args.get("interval") or "1d").lower()
    if interval not in INTERVALS:
        return ToolResult.failed(
            TOOL_ERROR, f"Interval must be one of {', '.join(INTERVALS)}.",
            reason="unsupported interval",
        )
    day_span = days_arg(args, DEFAULT_DAYS, MIN_DAYS, MAX_DAYS)

    try:
        async with _client() as client:
            symbol = await _resolve(client, raw) or raw.upper()
            bars, meta = await _bars(client, symbol, day_span + TREND_PREHISTORY[interval], interval)
    except (Unavailable, httpx.HTTPError) as e:
        return _yahoo_error(e, f"No {INTERVALS[interval]} history came back for {raw}: ")

    wording = INTERVALS[interval]
    if len(bars) < MIN_BARS:
        return ToolResult.failed(
            TOOL_ERROR,
            f"Only {len(bars)} {wording} bars came back for {symbol}, "
            f"too few to read a trend. {MIN_BARS} are needed; ask for a longer "
            "window.",
            reason="not enough history",
        )

    name = meta.get("shortName") or meta.get("longName") or symbol
    currency = meta.get("currency") or ""
    exchange = meta.get("fullExchangeName") or meta.get("exchangeName") or ""
    figures = analyse(bars)
    cutoff = bars[-1]["t"] - day_span * 86_400
    visible = [bar for bar in bars if bar["t"] > cutoff]
    subject = f"{name} ({symbol}) on {exchange}, {wording}, {len(visible)} chart bars"
    text = trend_prose(
        subject,
        figures,
        close=f"{money(figures['price'])} {currency}",
        source=f"Yahoo Finance {wording} candles",
        unit="session" if interval == "1d" else "bar",
    )
    if figures["ma50"] is None:
        text += (f"\nMA50 is unavailable. It requires 50 {wording} closes, and the source "
                 f"returned {len(bars)}. Do not infer the close's position relative to MA50.")
    url = PAGE_URL.format(symbol=symbol)
    card = trend_card(figures, visible, symbol=symbol, quote=currency, source="Yahoo Finance",
                      url=url, interval=interval, name=name, exchange=exchange, history=bars)
    row = source_row(url, f"{symbol} {wording} — Yahoo Finance")
    return ToolResult.ok(text, sources=[row], card=card)


_RATE_LIMITED = "Yahoo Finance is rate-limiting right now. Try again in a minute."
_UNREACHABLE = "Yahoo Finance did not answer in time."
# A session Yahoo itself refused is neither a timeout nor a bad symbol, so it
# gets its own sentence instead of borrowing either one.
_NO_SESSION = "Yahoo Finance refused the session, so nothing could be read."


def _yahoo_error(e, missing=""):
    """The tool's answer when a Yahoo call could not be made. A transport
    failure and a refused session both mean Yahoo never answered, so neither
    wears the caller's "no history for X" prefix; a rate limit gets its own
    sentence; anything else is the caller's words about the symbol itself."""
    if isinstance(e, httpx.HTTPError):
        return ToolResult.failed(TOOL_ERROR, _UNREACHABLE, reason="yahoo did not answer")
    if e.limited:
        return ToolResult.failed(TOOL_ERROR, _RATE_LIMITED, reason="yahoo is rate-limiting")
    if e.unreached:
        return ToolResult.failed(TOOL_ERROR, _NO_SESSION, reason="yahoo refused the session")
    return ToolResult.failed(TOOL_ERROR, missing + f"{e.reason}." if missing else
                             f"Yahoo Finance could not be reached: {e.reason}.",
                             reason=e.reason)


def _region(value):
    text = str(value or "").strip().lower()
    if not text:
        return "id"
    if text in REGIONS:
        return text
    return REGION_ALIASES.get(text, "")


async def read_screen(args):
    """Answer the screener tool, or say which failure stopped it."""
    region = _region(args.get("region"))
    if not region:
        names = ", ".join(f"{code} ({name})" for code, name in sorted(REGIONS.items()))
        return ToolResult.failed(
            TOOL_ERROR,
            f"No screen exists for that market. Yahoo's screener works one "
            f"exchange at a time; the ones it covers are {names}. A screen "
            f"across every market at once is not something it offers.",
            reason="no screen for that market",
        )

    sort = str(args.get("sort") or "gainers").strip().lower()
    if sort not in SORTS:
        return ToolResult.failed(
            TOOL_ERROR, f"Sort must be one of {', '.join(SORTS)}.",
            reason="unsupported sort",
        )
    try:
        limit = int(args.get("limit") or SCREEN_DEFAULT)
    except (TypeError, ValueError):
        limit = SCREEN_DEFAULT
    limit = min(max(limit, SCREEN_MIN), SCREEN_MAX)

    field, direction, wording = SORTS[sort]
    query = {
        "operator": "AND",
        "operands": [{"operator": "EQ", "operands": ["region", region]}],
    }
    body = {
        "size": limit,
        "offset": 0,
        "sortField": field,
        "sortType": direction,
        "quoteType": "EQUITY",
        "topOperator": "AND",
        "query": query,
        "userId": "",
        "userIdType": "guid",
    }

    try:
        async with _client() as client:
            d = await _json(client, SCREEN_URL,
                            {"lang": "en-US", "region": "US"},
                            guarded=True, method="POST", body=body)
    except (Unavailable, httpx.HTTPError) as e:
        return _yahoo_error(e)

    result = ((d.get("finance") or {}).get("result") or [None])[0]
    if not result:
        return ToolResult.failed(
            TOOL_ERROR, f"Yahoo's screen for {REGIONS[region]} came back empty.",
            reason="the screen came back empty",
        )

    rows = []
    for q in result.get("quotes") or []:
        row = _row(q)
        if row["price"] is None:
            continue
        rows.append(row)
    if not rows:
        return ToolResult.failed(
            TOOL_ERROR, f"No priced rows came back for {REGIONS[region]}.",
            reason="the screen came back empty",
        )

    market_name = REGIONS[region]
    text = (f"{wording.capitalize()} on {market_name}, {len(rows)} of "
            f"{result.get('total') or len(rows)} listings:\n")
    for r in rows:
        line = f"- {r['symbol']} {r['name']}: {money(r['price'])} {r['quote'] or ''}".rstrip()
        if r["change"] is not None:
            line += f", {r['change']:+.2f}%"
        if r["volume"] is not None:
            line += f", volume {big(r['volume'])}"
        text += line + "\n"
    text += (
        "Yahoo's ranking, read as it stood when the tool ran. Say the list is a "
        "snapshot rather than the whole market, and that a listing whose price "
        "did not come back was left out."
    )

    card = {
        "region": region,
        "market": market_name,
        "sort": sort,
        "total": result.get("total"),
        "rows": rows,
        "source": "Yahoo Finance",
        "at": int(time.time()),
    }
    row = source_row(PAGE_URL.format(symbol=rows[0]["symbol"]),
                     f"{wording.capitalize()} — {market_name}")
    return ToolResult.ok(text[:MAX_CHARS], sources=[row], card=card)


QUOTE_DESCRIPTION = (
    "Current price of a share, index or ETF, with today's move, the day "
    "and 52-week ranges, volume, market cap, P/E and dividend yield. "
    "Covers the exchanges Yahoo Finance lists, which is most of the "
    "world: give a name ('bank mandiri', 'toyota') or a ticker with its "
    "exchange ('BBCA.JK', '7203.T', '^JKSE'). Several symbols may be "
    "asked for in one call. Read-only."
)
TREND_DESCRIPTION = (
    "Read the trend of a share or index from its candles and draw them: "
    "moving averages, RSI, volatility, where price sits in its recent "
    "range, and the move over 7, 30 and 90 bars. Use it when asked "
    "whether a stock is trending, stretched or worth watching, or when "
    "a chart answers better than a sentence. Read-only. It describes "
    "past closes and does not forecast."
)
SCREEN_DESCRIPTION = (
    "List shares on one exchange by how they moved: biggest gainers, "
    "biggest losers, most traded, or largest by market cap. Use it for "
    "'what is moving in Indonesia today' or 'the biggest shares on the "
    "IDX'. It ranks listings Yahoo Finance covers; it is a snapshot, not "
    "the whole market. Read-only."
)

QUOTE_PARAMETERS = {
    "type": "object",
    "properties": {
        "symbol": {
            "type": "string",
            "description": (
                f"One symbol, or up to {MAX_SYMBOLS} separated by commas. A "
                "name is searched, a ticker is taken as written. "
                f"{', '.join(repr(n) for n in ALIAS_NAMES)} are recognised."
            ),
        },
    },
    "required": ["symbol"],
}
TREND_PARAMETERS = {
    "type": "object",
    "properties": {
        "symbol": {
            "type": "string",
            "description": "The listing: 'BBCA.JK', '^JKSE', 'AAPL', or a name.",
        },
        "interval": {
            "type": "string",
            "enum": list(INTERVALS),
            "description": "Bar size. '1d' daily, '1wk' weekly, '1mo' monthly. Default 1d.",
        },
        "days": {
            "type": "integer",
            "description": f"Calendar days shown on the chart, {MIN_DAYS} to "
                           f"{MAX_DAYS}. Default {DEFAULT_DAYS}. Moving averages use "
                           "earlier closes when available.",
        },
    },
    "required": ["symbol"],
}
SCREEN_PARAMETERS = {
    "type": "object",
    "properties": {
        "region": {
            "type": "string",
            "description": (
                f"Market: 'id' for Indonesia, or {', '.join(REGIONS)}. Default id."
            ),
        },
        "sort": {
            "type": "string",
            "enum": list(SORTS),
            "description": f"{', '.join(SORTS)}. Default gainers.",
        },
        "limit": {
            "type": "integer",
            "description": (
                f"Rows to return, {SCREEN_MIN} to {SCREEN_MAX}. Default {SCREEN_DEFAULT}."
            ),
        },
    },
}


def register():
    registry.add(Tool(
        name=NAME,
        description=QUOTE_DESCRIPTION,
        parameters=QUOTE_PARAMETERS,
        handler=read,
        permission=EXTERNAL,
        timeout=TIMEOUT_S,
        tags=TAGS,
        carded=True,
    ))
    registry.add(Tool(
        name=TREND_NAME,
        description=TREND_DESCRIPTION,
        parameters=TREND_PARAMETERS,
        handler=read_trend,
        permission=EXTERNAL,
        timeout=TREND_TIMEOUT_S,
        tags=TAGS,
        carded=True,
    ))
    registry.add(Tool(
        name=SCREEN_NAME,
        description=SCREEN_DESCRIPTION,
        parameters=SCREEN_PARAMETERS,
        handler=read_screen,
        permission=EXTERNAL,
        timeout=SCREEN_TIMEOUT_S,
        tags=TAGS,
        carded=True,
    ))

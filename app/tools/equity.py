"""Equities: quotes, daily candles and screeners, from Yahoo Finance.

Yahoo documents none of this. The quotes and the screener are guarded by a
cookie and a crumb, so that pair is fetched once, kept, and fetched again when
a call comes back 401. The chart endpoint needs neither, which is why the trend
reading still answers when the handshake itself is broken. Nothing here places
an order: every request reads a public page of market data.
"""

import re
import time

import httpx
from contextlib import asynccontextmanager

from . import as_args, crypto, market
from .fetch import USER_AGENT

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
MIN_BARS = market.MIN_BARS

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
}

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


class Unavailable(Exception):
    """One source could not answer. `limited` tells a rate limit apart from a
    symbol Yahoo does not list."""

    def __init__(self, reason, limited=False):
        super().__init__(reason)
        self.reason = reason
        self.limited = limited


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pos(value):
    """A figure, or `None` when there is nothing to report. A zero volume on a
    halted ticker is not a volume, and printed as one it is a wrong fact."""
    return value or None


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
                raise Unavailable("yahoo refused the session")
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
        return r.json()
    raise Unavailable("yahoo refused the session")


def _search(client, text):
    return _json(client, SEARCH_URL, {"q": text, "quotesCount": 8, "newsCount": 0})


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
        d = await _json(client, QUOTE_URL,
                        {"symbols": ",".join(chunk)}, guarded=True)
        rows.extend((d.get("quoteResponse") or {}).get("result") or [])
    return rows


def _row(q):
    symbol = q.get("symbol") or ""
    return {
        "symbol": symbol,
        "name": q.get("shortName") or q.get("longName") or symbol,
        "quote": q.get("currency"),
        "price": _num(q.get("regularMarketPrice")),
        "change": _num(q.get("regularMarketChangePercent")),
        "high": _pos(_num(q.get("regularMarketDayHigh"))),
        "low": _pos(_num(q.get("regularMarketDayLow"))),
        "prev": _pos(_num(q.get("regularMarketPreviousClose"))),
        "volume": _pos(_num(q.get("regularMarketVolume"))),
        "market_cap": _pos(_num(q.get("marketCap"))),
        "pe": _num(q.get("trailingPE")),
        "dividend": _num(q.get("dividendYield")),
        "week52_high": _pos(_num(q.get("fiftyTwoWeekHigh"))),
        "week52_low": _pos(_num(q.get("fiftyTwoWeekLow"))),
        "exchange": q.get("fullExchangeName") or q.get("exchange"),
        "kind": q.get("quoteType"),
        "market_state": q.get("marketState"),
        "url": PAGE_URL.format(symbol=symbol),
    }


def _render_rows(rows):
    lines = []
    for r in rows:
        line = f"{r['name']} ({r['symbol']}) on {r['exchange']}: "
        line += crypto.money(r["price"]) + (f" {r['quote']}" if r["quote"] else "")
        if r["change"] is not None:
            line += f", {r['change']:+.2f}% today"
        line += "."
        facts = []
        if r["prev"] is not None:
            facts.append(f"previous close {crypto.money(r['prev'])}")
        if r["high"] is not None and r["low"] is not None:
            facts.append(f"day range {crypto.money(r['low'])} to {crypto.money(r['high'])}")
        if r["week52_low"] is not None and r["week52_high"] is not None:
            facts.append(f"52-week range {crypto.money(r['week52_low'])} to "
                         f"{crypto.money(r['week52_high'])}")
        if r["volume"] is not None:
            facts.append(f"volume {crypto.big(r['volume'])}")
        if r["market_cap"] is not None:
            facts.append(f"market cap {crypto.big(r['market_cap'])} {r['quote'] or ''}".strip())
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
        f"exchanges. Read at {crypto.when(int(time.time()))}. If a symbol looks "
        "like the wrong company, say which exchange you meant rather than "
        "guessing."
    )
    return "\n".join(lines)[:MAX_CHARS]


async def read(args):
    """`(ok, text, reason, meta)` for the quote tool."""
    args = as_args(args)
    raw = args.get("symbol") or args.get("symbols") or ""
    parts = [p.strip() for p in re.split(r"[,;\n]+", str(raw)) if p.strip()]
    if not parts:
        return False, "No symbol given.", "no symbol was given", {}
    if len(parts) > MAX_SYMBOLS:
        return (False,
                f"{len(parts)} symbols were asked for; {MAX_SYMBOLS} is the most one "
                "call reads. Ask again for the rest.",
                "too many symbols in one call", {})

    try:
        async with _client() as client:
            symbols = []
            missing = []
            for part in parts:
                symbol = await _resolve(client, part)
                (symbols if symbol else missing).append(symbol or part)
            if not symbols:
                text = (f"Nothing on Yahoo Finance matches {', '.join(parts)}. "
                        "Give a ticker with its exchange, such as BBCA.JK or 7203.T.")
                return False, text, "no listing matches that name", {}

            rows = [_row(q) for q in await _quote_rows(client, symbols)]
            found = {r["symbol"] for r in rows}
            missing.extend(s for s in symbols if s not in found)
            rows = [r for r in rows if r["price"] is not None]
    except Unavailable as e:
        if e.limited:
            text = "Yahoo Finance is rate-limiting right now. Try again in a minute."
            return False, text, "yahoo is rate-limiting", {}
        text = f"Yahoo Finance could not be reached: {e.reason}."
        return False, text, e.reason, {}
    except httpx.HTTPError:
        return (False, "Yahoo Finance did not answer in time.",
                "yahoo did not answer", {})

    if not rows:
        text = ("Yahoo Finance listed " + ", ".join(symbols) +
                " but sent no price for it, which usually means the listing is "
                "delisted or halted.")
        return False, text, "no price came back", {}

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
    source_row = {
        "source": rows[0]["url"],
        "title": f"{rows[0]['symbol']} — Yahoo Finance",
        "kind": "web",
        "read": False,
    }
    return True, text, "", {"sources": [source_row], "card": card}


def _days(args):
    try:
        days = int(args.get("days") or DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return min(max(days, MIN_DAYS), MAX_DAYS)


async def _bars(client, symbol, days, interval):
    now = int(time.time())
    d = await _json(client, CHART_URL.format(symbol=symbol),
                    {"period1": now - days * 86_400, "period2": now,
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


async def read_trend(args):
    """`(ok, text, reason, meta)` for the equity trend tool."""
    args = as_args(args)
    raw = str(args.get("symbol") or "").strip()
    if not raw:
        return False, "No symbol given.", "no symbol was given", {}
    interval = str(args.get("interval") or "1d").lower()
    if interval not in INTERVALS:
        return (False, f"Interval must be one of {', '.join(INTERVALS)}.",
                "unsupported interval", {})
    days = _days(args)

    try:
        async with _client() as client:
            symbol = await _resolve(client, raw) or raw.upper()
            bars, meta = await _bars(client, symbol, days, interval)
    except Unavailable as e:
        if e.limited:
            text = "Yahoo Finance is rate-limiting right now. Try again in a minute."
            return False, text, "yahoo is rate-limiting", {}
        text = f"No daily history came back for {raw}: {e.reason}."
        return False, text, e.reason, {}
    except httpx.HTTPError:
        return False, "Yahoo Finance did not answer in time.", "yahoo did not answer", {}

    if len(bars) < MIN_BARS:
        text = (f"Only {len(bars)} {INTERVALS[interval]} bars came back for {symbol}, "
                f"too few to read a trend. {MIN_BARS} are needed; ask for a longer "
                "window.")
        return False, text, "not enough history", {}

    name = meta.get("shortName") or meta.get("longName") or symbol
    currency = meta.get("currency") or ""
    exchange = meta.get("fullExchangeName") or meta.get("exchangeName") or ""
    a = market.analyse(bars)
    verdict = market.trend_of(a)
    condition = market.condition_of(a["rsi"])

    closes = [b["c"] for b in bars]
    parts = []
    if a["ma20"] is not None and a["above20"] is not None:
        side = "above" if a["above20"] >= 0 else "below"
        line = f"MA20 {crypto.money(a['ma20'])} ({a['above20']:+.2f}% {side})"
        if a["slope20"] is not None:
            line += f", MA20 slope {a['slope20']:+.2f}% over the last 10 bars"
        parts.append(line)
    if a["ma50"] is not None and a["above50"] is not None:
        side = "above" if a["above50"] >= 0 else "below"
        parts.append(f"MA50 {crypto.money(a['ma50'])} ({a['above50']:+.2f}% {side})")
    if a["ma200"] is not None and a["above200"] is not None:
        side = "above" if a["above200"] >= 0 else "below"
        parts.append(f"MA200 {crypto.money(a['ma200'])} ({a['above200']:+.2f}% {side})")
    changes = [
        f"{label} {a[key]:+.2f}%"
        for key, label in (("change_7", "7 bars"), ("change_30", "30 bars"),
                           ("change_90", "90 bars"))
        if a[key] is not None
    ]
    if changes:
        parts.append(", ".join(changes))
    if a["range_pos"] is not None:
        parts.append(f"30-bar range {crypto.money(a['range_low'])} to "
                     f"{crypto.money(a['range_high'])}, price at {a['range_pos']:.0f}% of it")
    if a["atr_pct"] is not None:
        parts.append(f"ATR14 {a['atr_pct']:.2f}% of price")
    if a["rel_volume"] is not None:
        parts.append(f"volume {a['rel_volume']:.2f}× its 20-bar average")

    head = f"{name} ({symbol}) on {exchange}, {INTERVALS[interval]}, {len(bars)} bars, {verdict}"
    if condition and condition != "neutral":
        head += f", RSI {a['rsi']} ({condition})"
    lines = [head + f". Last close {crypto.money(a['price'])} {currency}."]
    if parts:
        lines.append("; ".join(parts) + ".")
    lines.append(
        "These are measurements of past closes, not a forecast. Say so if the "
        "answer reads as a prediction. The reader sees the candles, so name a "
        "couple of closing prices rather than describing every bar."
    )
    lines.append(f"Source: Yahoo Finance {INTERVALS[interval]} candles, as of "
                 f"{crypto.when(int(time.time()))}.")
    text = "\n".join(lines)[:MAX_CHARS]

    card = {
        "symbol": symbol,
        "quote": currency,
        "name": name,
        "exchange": exchange,
        "verdict": verdict,
        "condition": condition,
        "price": a["price"],
        "ma20": a["ma20"],
        "ma50": a["ma50"],
        "ma200": a["ma200"],
        "above20": a["above20"],
        "above50": a["above50"],
        "above200": a["above200"],
        "slope20": a["slope20"],
        "rsi": a["rsi"],
        "atr_pct": a["atr_pct"],
        "range_high": a["range_high"],
        "range_low": a["range_low"],
        "range_pos": a["range_pos"],
        "change_7": a["change_7"],
        "change_30": a["change_30"],
        "change_90": a["change_90"],
        "rel_volume": a["rel_volume"],
        "bar_count": len(bars),
        "interval": interval,
        "source": "Yahoo Finance",
        "url": PAGE_URL.format(symbol=symbol),
        "at": int(time.time()),
        "candles": [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars],
        "ma20_series": market.ma_series(closes, 20),
        "ma50_series": market.ma_series(closes, 50),
        "chart_high": max(b["h"] for b in bars),
        "chart_low": min(b["l"] for b in bars),
        "change_window": market.change_of(closes, len(closes) - 1),
    }
    source_row = {
        "source": card["url"],
        "title": f"{symbol} {INTERVALS[interval]} — Yahoo Finance",
        "kind": "web",
        "read": False,
    }
    return True, text, "", {"sources": [source_row], "card": card}


def _region(value):
    text = str(value or "").strip().lower()
    if not text:
        return "id"
    if text in REGIONS:
        return text
    return REGION_ALIASES.get(text, "")


async def read_screen(args):
    """`(ok, text, reason, meta)` for the screener tool."""
    args = as_args(args)
    region = _region(args.get("region"))
    if not region:
        names = ", ".join(f"{code} ({name})" for code, name in sorted(REGIONS.items()))
        return (False,
                f"No screen exists for that market. Yahoo's screener works one "
                f"exchange at a time; the ones it covers are {names}. A screen "
                f"across every market at once is not something it offers.",
                "no screen for that market", {})

    sort = str(args.get("sort") or "gainers").strip().lower()
    if sort not in SORTS:
        return (False, f"Sort must be one of {', '.join(SORTS)}.",
                "unsupported sort", {})
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = min(max(limit, 5), 50)

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
    except Unavailable as e:
        if e.limited:
            text = "Yahoo Finance is rate-limiting right now. Try again in a minute."
            return False, text, "yahoo is rate-limiting", {}
        text = f"Yahoo Finance could not be reached: {e.reason}."
        return False, text, e.reason, {}
    except httpx.HTTPError:
        return False, "Yahoo Finance did not answer in time.", "yahoo did not answer", {}

    result = ((d.get("finance") or {}).get("result") or [None])[0]
    if not result:
        return (False, f"Yahoo's screen for {REGIONS[region]} came back empty.",
                "the screen came back empty", {})

    rows = []
    for q in result.get("quotes") or []:
        if q.get("regularMarketPrice") is None:
            continue
        rows.append({
            "symbol": q.get("symbol") or "",
            "name": q.get("shortName") or q.get("longName") or q.get("symbol") or "",
            "quote": q.get("currency"),
            "price": _num(q.get("regularMarketPrice")),
            "change": _num(q.get("regularMarketChangePercent")),
            "volume": _pos(_num(q.get("regularMarketVolume"))),
            "market_cap": _pos(_num(q.get("marketCap"))),
            "url": PAGE_URL.format(symbol=q.get("symbol") or ""),
        })
    if not rows:
        return (False, f"No priced rows came back for {REGIONS[region]}.",
                "the screen came back empty", {})

    market_name = REGIONS[region]
    text = (f"{wording.capitalize()} on {market_name}, {len(rows)} of "
            f"{result.get('total') or len(rows)} listings:\n")
    for r in rows:
        line = f"- {r['symbol']} {r['name']}: {crypto.money(r['price'])} {r['quote'] or ''}".rstrip()
        if r["change"] is not None:
            line += f", {r['change']:+.2f}%"
        if r["volume"] is not None:
            line += f", volume {crypto.big(r['volume'])}"
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
    source_row = {
        "source": PAGE_URL.format(symbol=rows[0]["symbol"]),
        "title": f"{wording.capitalize()} — {market_name}",
        "kind": "web",
        "read": False,
    }
    return True, text[:MAX_CHARS], "", {"sources": [source_row], "card": card}


TOOL = {
    "type": "function",
    "function": {
        "name": "equity",
        "description": (
            "Current price of a share, index or ETF, with today's move, the day "
            "and 52-week ranges, volume, market cap, P/E and dividend yield. "
            "Covers the exchanges Yahoo Finance lists, which is most of the "
            "world: give a name ('bank mandiri', 'toyota') or a ticker with its "
            "exchange ('BBCA.JK', '7203.T', '^JKSE'). Several symbols may be "
            "asked for in one call. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": (
                        "One symbol, or up to ten separated by commas. A name is "
                        "searched, a ticker is taken as written. 'IHSG' and "
                        "'LQ45' are recognised."
                    ),
                },
            },
            "required": ["symbol"],
        },
    },
}


TREND_TOOL = {
    "type": "function",
    "function": {
        "name": "equity_trend",
        "description": (
            "Read the trend of a share or index from its candles and draw them: "
            "moving averages, RSI, volatility, where price sits in its recent "
            "range, and the move over 7, 30 and 90 bars. Use it when asked "
            "whether a stock is trending, stretched or worth watching, or when "
            "a chart answers better than a sentence. Read-only. It describes "
            "past closes and does not forecast."
        ),
        "parameters": {
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
                    "description": f"Calendar days of history to read, {MIN_DAYS} to "
                                   f"{MAX_DAYS}. Default {DEFAULT_DAYS}.",
                },
            },
            "required": ["symbol"],
        },
    },
}


SCREEN_TOOL = {
    "type": "function",
    "function": {
        "name": "screen",
        "description": (
            "List shares on one exchange by how they moved: biggest gainers, "
            "biggest losers, most traded, or largest by market cap. Use it for "
            "'what is moving in Indonesia today' or 'the biggest shares on the "
            "IDX'. It ranks listings Yahoo Finance covers; it is a snapshot, not "
            "the whole market. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": (
                        "Market: 'id' for Indonesia, or us, jp, hk, gb, de, in, "
                        "br, ca, au, sg, kr, tw, my, th. Default id."
                    ),
                },
                "sort": {
                    "type": "string",
                    "enum": list(SORTS),
                    "description": "gainers, losers, active or largest. Default gainers.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Rows to return, 5 to 50. Default 10.",
                },
            },
        },
    },
}


async def run(args):
    return (await read(args))[1]


async def run_trend(args):
    return (await read_trend(args))[1]


async def run_screen(args):
    return (await read_screen(args))[1]
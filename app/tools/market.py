"""Daily candles, and the trend figures read from them.

The model cannot count a hundred candles, so none of that is asked of it: the
arithmetic happens in `marketdata` and the model only writes the sentence around
numbers it was handed. Bars come from Binance, then Gate.io, then Coinbase.
Binance's own market-data host is asked first and again second: on a network
that blocks `api.binance.com` by name, `data-api.binance.vision` still answers,
and it serves the same rows. CoinGecko's free OHLC endpoint is four-hourly, so
it cannot serve a daily chart and is not asked.
"""

import asyncio

import httpx

from ..errors import TOOL_ERROR
from ..security import EXTERNAL
from .marketdata import (
    DEFAULT_QUOTE, MIN_BARS, Unavailable, analyse, binance_pair, coinbase_pair,
    gate_pair, money, no_source, parse_quote, trend_card, trend_prose,
)
from .registry import TAGS, Tool, ToolResult, days_arg, missing_arg, registry, source_row

KLINE_URL = "https://api.binance.com/api/v3/klines"
# Binance's market-data-only host. Same rows and same shape, served from a name
# that a DNS filter aimed at `api.binance.com` does not cover.
VISION_URL = "https://data-api.binance.vision/api/v3/klines"
GATE_URL = "https://api.gateio.ws/api/v4/spot/candlesticks"
CANDLE_URL = "https://api.exchange.coinbase.com/products/{pair}/candles"
TIMEOUT = 12
DEFAULT_DAYS = 90
MIN_DAYS = 30
MAX_DAYS = 365
NAME = "crypto_trend"
TIMEOUT_S = 35.0

DESCRIPTION = (
    "Read the daily trend of a cryptocurrency and draw its candles: "
    "moving averages, RSI, volatility, where price sits in its 30-day "
    "range, and the move over 7, 30 and 90 days, with the same bars shown "
    "as a chart. Use it when asked whether a coin is bullish, bearish, "
    "trending or stretched, or when a picture of the price would answer "
    "better than a sentence. Read-only. It describes past closes and "
    "does not forecast."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string",
                   "description": "The coin: 'BTC', 'ETH', or 'BTC/USDT'."},
        "quote": {"type": "string",
                  "description": f"The currency to price it in. Default {DEFAULT_QUOTE}."},
        "days": {"type": "integer",
                 "description": f"Daily bars to read, {MIN_DAYS} to {MAX_DAYS}. "
                                f"Default {DEFAULT_DAYS}."},
    },
    "required": ["symbol"],
}


def _binance_bar(row):
    return {
        "t": int(row[0]) // 1000,
        "o": float(row[1]),
        "h": float(row[2]),
        "l": float(row[3]),
        "c": float(row[4]),
        "v": float(row[5]),
    }


def _coinbase_bar(row):
    # [time, low, high, open, close, volume], newest first.
    return {
        "t": int(row[0]),
        "o": float(row[3]),
        "h": float(row[2]),
        "l": float(row[1]),
        "c": float(row[4]),
        "v": float(row[5]),
    }


async def _binance_bars(client, url, base, quote, bar_count):
    pair = binance_pair(base, quote)
    r = await client.get(url, params={"symbol": pair, "interval": "1d", "limit": bar_count})
    if r.status_code in (418, 429):
        raise Unavailable("binance is rate-limiting", limited=True)
    if r.status_code == 400:
        raise Unavailable("binance has no such pair")
    r.raise_for_status()
    bars = [b for b in (_binance_bar(row) for row in r.json()) if b["c"] > 0]
    if not bars:
        raise Unavailable("binance sent no candles")
    return bars


def _gate_bar(row):
    # [time, quote volume, close, high, low, open, base volume, closed], oldest
    # first. Gate.io quotes in USDT where a question says USD, so it is asked
    # through the same mapping Binance uses.
    return {
        "t": int(row[0]),
        "o": float(row[5]),
        "h": float(row[3]),
        "l": float(row[4]),
        "c": float(row[2]),
        "v": float(row[6]),
    }


async def _gate_bars(client, base, quote, bar_count):
    r = await client.get(GATE_URL, params={
        "currency_pair": gate_pair(base, quote), "interval": "1d", "limit": bar_count,
    })
    if r.status_code == 429:
        raise Unavailable("gate.io is rate-limiting", limited=True)
    if r.status_code == 400:
        raise Unavailable("gate.io has no such pair")
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list):
        raise Unavailable("gate.io sent no candles")
    bars = [b for b in (_gate_bar(row) for row in rows) if b["c"] > 0]
    if not bars:
        raise Unavailable("gate.io sent no candles")
    return bars[-bar_count:]


async def _coinbase_bars(client, base, quote, bar_count):
    url = CANDLE_URL.format(pair=coinbase_pair(base, quote))
    r = await client.get(url, params={"granularity": 86400})
    if r.status_code == 429:
        raise Unavailable("coinbase is rate-limiting", limited=True)
    if r.status_code == 404:
        raise Unavailable("coinbase has no such pair")
    r.raise_for_status()
    bars = sorted((_coinbase_bar(row) for row in r.json()), key=lambda b: b["t"])
    bars = [b for b in bars if b["c"] > 0][-bar_count:]
    if not bars:
        raise Unavailable("coinbase sent no candles")
    return bars


async def _gather(client, base, quote, bar_count):
    """`(bars oldest-first, source, url)`, or raise `Unavailable`."""
    failures = []

    async def ask(name, url, fetch):
        try:
            return await fetch()
        except Unavailable as e:
            failures.append(e)
        except (httpx.HTTPError, ValueError, TypeError, IndexError) as e:
            # A transport failure means the host never answered. Recorded as
            # such so the last sentence does not call a blocked network a pair
            # that no exchange lists.
            failures.append(Unavailable(
                f"{name} could not be read", unreached=isinstance(e, httpx.TransportError),
            ))
        return None

    checks = (
        ("Binance", KLINE_URL,
         lambda: _binance_bars(client, KLINE_URL, base, quote, bar_count)),
        # Same rows under a host a name-based filter is less likely to cover.
        ("Binance", VISION_URL,
         lambda: _binance_bars(client, VISION_URL, base, quote, bar_count)),
        ("Gate.io", GATE_URL,
         lambda: _gate_bars(client, base, quote, bar_count)),
        ("Coinbase", CANDLE_URL.format(pair=coinbase_pair(base, quote)),
         lambda: _coinbase_bars(client, base, quote, bar_count)),
    )
    # All four at once, not one after another. Each has its own twelve-second
    # ceiling, so asking in turn could spend forty-eight seconds on a network
    # that is simply slow, and the tool's own budget is below that: the run
    # would be cut off with the answer already on its way. Together they cost
    # the slowest one, and `ask` already turns every failure into a row here.
    answers = await asyncio.gather(*(ask(name, url, fetch) for name, url, fetch in checks))
    # Read back in the order the sources are wanted, so a pair listed in two
    # places is still credited to the first one asked.
    for (name, url, _), bars in zip(checks, answers):
        if bars:
            return bars, name, url

    if failures and all(f.limited for f in failures):
        raise Unavailable("every candle source is rate-limiting", limited=True)
    raise Unavailable(
        "no candle source lists that pair",
        unreached=bool(failures) and all(f.unreached for f in failures),
    )


async def _load(args):
    """`(base, quote, bars, source, url)` or a `ToolResult` saying why not."""
    base, quote = parse_quote(args)
    if not base:
        return missing_arg("coin")
    bar_count = days_arg(args, DEFAULT_DAYS, MIN_DAYS, MAX_DAYS)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            bars, source, url = await _gather(client, base, quote, bar_count)
    except Unavailable as e:
        text, reason = no_source(e.limited, "candle", f"{base} against {quote}",
                                 unreached=e.unreached)
        return ToolResult.failed(TOOL_ERROR, text, reason=reason)
    if len(bars) < MIN_BARS:
        return ToolResult.failed(
            TOOL_ERROR,
            f"Only {len(bars)} daily bars came back for {base}/{quote}, too few to "
            f"read a trend. {MIN_BARS} are needed.",
            reason="not enough daily history",
        )
    return base, quote, bars, source, url


async def read(args):
    """Answer the trend tool, or say which failure stopped it."""
    loaded = await _load(args)
    if isinstance(loaded, ToolResult):
        return loaded
    base, quote, bars, source_name, url = loaded
    figures = analyse(bars)
    text = trend_prose(
        f"{base}/{quote} daily, {len(bars)} bars",
        figures,
        close=f"{money(figures['price'])} {quote}",
        source=f"{source_name} daily candles",
    )
    row = source_row(url, f"{base}/{quote} daily — {source_name}")
    return ToolResult.ok(
        text,
        sources=[row],
        card=trend_card(figures, bars, symbol=base, quote=quote,
                        source=source_name, url=url),
    )


def register():
    return registry.add(Tool(
        name=NAME,
        description=DESCRIPTION,
        parameters=PARAMETERS,
        handler=read,
        permission=EXTERNAL,
        timeout=TIMEOUT_S,
        tags=TAGS,
        carded=True,
    ))

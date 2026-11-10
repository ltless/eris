"""Daily candles, and the trend figures read from them.

The model cannot count a hundred candles, so none of that is asked of it: the
arithmetic happens in `marketdata` and the model only writes the sentence around
numbers it was handed. Bars come from Binance, then Coinbase. CoinGecko's free
OHLC endpoint is four-hourly, so it cannot serve a daily chart and is not asked.
"""

import httpx

from ..errors import TOOL_ERROR
from ..security import EXTERNAL
from .marketdata import (
    DEFAULT_QUOTE, MIN_BARS, Unavailable, analyse, binance_pair, coinbase_pair, money,
    no_source, parse_quote, trend_card, trend_prose,
)
from .registry import TAGS, Tool, ToolResult, days_arg, missing_arg, registry, source_row

KLINE_URL = "https://api.binance.com/api/v3/klines"
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


async def _gather(client, base, quote, bar_count):
    """`(bars oldest-first, source, url)`, or raise `Unavailable`."""
    failures = []
    pair = binance_pair(base, quote)
    try:
        r = await client.get(KLINE_URL,
                             params={"symbol": pair, "interval": "1d", "limit": bar_count})
        if r.status_code in (418, 429):
            raise Unavailable("binance is rate-limiting", limited=True)
        if r.status_code == 400:
            raise Unavailable("binance has no such pair")
        r.raise_for_status()
        bars = [_binance_bar(row) for row in r.json()]
        bars = [b for b in bars if b["c"] > 0]
        if bars:
            return bars, "Binance", KLINE_URL
        failures.append(Unavailable("binance sent no candles"))
    except Unavailable as e:
        failures.append(e)
    except (httpx.HTTPError, ValueError, TypeError, IndexError):
        failures.append(Unavailable("binance could not be read"))

    pair = coinbase_pair(base, quote)
    url = CANDLE_URL.format(pair=pair)
    try:
        r = await client.get(url, params={"granularity": 86400})
        if r.status_code == 429:
            raise Unavailable("coinbase is rate-limiting", limited=True)
        if r.status_code == 404:
            raise Unavailable("coinbase has no such pair")
        r.raise_for_status()
        bars = sorted((_coinbase_bar(row) for row in r.json()), key=lambda b: b["t"])
        bars = [b for b in bars if b["c"] > 0][-bar_count:]
        if bars:
            return bars, "Coinbase", url
        failures.append(Unavailable("coinbase sent no candles"))
    except Unavailable as e:
        failures.append(e)
    except (httpx.HTTPError, ValueError, TypeError, IndexError):
        failures.append(Unavailable("coinbase could not be read"))

    if failures and all(f.limited for f in failures):
        raise Unavailable("every candle source is rate-limiting", limited=True)
    raise Unavailable("no candle source lists that pair")


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
        text, reason = no_source(e.limited, "candle", f"{base} against {quote}")
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

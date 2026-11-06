"""Daily candles, and the trend figures read from them.

The model cannot count a hundred candles, so none of that is asked of it: the
arithmetic happens here and the model only writes the sentence around numbers
it was handed. Bars come from Binance, then Coinbase. CoinGecko's free OHLC
endpoint is four-hourly, so it cannot serve a daily chart and is not asked.
"""

import time

import httpx

from . import as_args, crypto

KLINE_URL = "https://api.binance.com/api/v3/klines"
CANDLE_URL = "https://api.exchange.coinbase.com/products/{pair}/candles"
TIMEOUT = 12
MAX_CHARS = 3_000
DEFAULT_DAYS = 90
MIN_DAYS = 30
MAX_DAYS = 365
MIN_BARS = 20

# Indicators read off a close series. Wilder's smoothing is not used: the simple
# average is what the card states, and a first-value difference is not worth a
# second formula to test.
def _sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _ma_series(closes, n):
    """A moving average for every bar, `None` where there is not enough history."""
    out = []
    total = 0.0
    for i, close in enumerate(closes):
        total += close
        if i >= n:
            total -= closes[i - n]
        out.append(round(total / n, 8) if i >= n - 1 else None)
    return out


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for prev, cur in zip(closes[-n - 1:-1], closes[-n:]):
        move = cur - prev
        if move >= 0:
            gains += move
        else:
            losses -= move
    if losses == 0:
        return 100.0
    return round(100 - 100 / (1 + (gains / n) / (losses / n)), 1)


def _atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    ranges = []
    for prev, cur in zip(bars[-n - 1:-1], bars[-n:]):
        ranges.append(max(cur["h"] - cur["l"],
                          abs(cur["h"] - prev["c"]),
                          abs(cur["l"] - prev["c"])))
    return sum(ranges) / n


def _slope(values, n):
    """Percent change of the last two `n`-bar averages, or None."""
    if len(values) < n * 2:
        return None
    now = sum(values[-n:]) / n
    before = sum(values[-2 * n:-n]) / n
    if not before:
        return None
    return round((now - before) / before * 100, 2)


def _change(closes, n):
    if len(closes) <= n or not closes[-n - 1]:
        return None
    return round((closes[-1] - closes[-n - 1]) / closes[-n - 1] * 100, 2)


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


async def _gather(client, base, quote, days):
    """`(bars oldest-first, source, url, pair)`, or raise `_Unavailable`."""
    failures = []
    pair = base + crypto.BINANCE_QUOTE.get(quote, quote)
    try:
        r = await client.get(KLINE_URL,
                             params={"symbol": pair, "interval": "1d", "limit": days})
        if r.status_code in (418, 429):
            raise crypto.Unavailable("binance is rate-limiting", limited=True)
        if r.status_code == 400:
            raise crypto.Unavailable("binance has no such pair")
        r.raise_for_status()
        bars = [_binance_bar(row) for row in r.json()]
        bars = [b for b in bars if b["c"] > 0]
        if bars:
            return bars, "Binance", KLINE_URL, pair
        failures.append(crypto.Unavailable("binance sent no candles"))
    except crypto.Unavailable as e:
        failures.append(e)
    except (httpx.HTTPError, ValueError, TypeError, IndexError):
        failures.append(crypto.Unavailable("binance could not be read"))

    pair = f"{base}-{quote}"
    url = CANDLE_URL.format(pair=pair)
    try:
        r = await client.get(url, params={"granularity": 86400})
        if r.status_code == 429:
            raise crypto.Unavailable("coinbase is rate-limiting", limited=True)
        if r.status_code == 404:
            raise crypto.Unavailable("coinbase has no such pair")
        r.raise_for_status()
        bars = sorted((_coinbase_bar(row) for row in r.json()), key=lambda b: b["t"])
        bars = [b for b in bars if b["c"] > 0][-days:]
        if bars:
            return bars, "Coinbase", url, pair
        failures.append(crypto.Unavailable("coinbase sent no candles"))
    except crypto.Unavailable as e:
        failures.append(e)
    except (httpx.HTTPError, ValueError, TypeError, IndexError):
        failures.append(crypto.Unavailable("coinbase could not be read"))

    if failures and all(f.limited for f in failures):
        raise crypto.Unavailable("every candle source is rate-limiting", limited=True)
    raise crypto.Unavailable("no candle source lists that pair")


def _days(args, default=DEFAULT_DAYS):
    try:
        days = int(args.get("days") or default)
    except (TypeError, ValueError):
        days = default
    return min(max(days, MIN_DAYS), MAX_DAYS)


def _analyse(bars):
    """Every figure the two cards and the model's sentence are built from."""
    closes = [b["c"] for b in bars]
    price = closes[-1]
    ma20 = _sma(closes, 20)
    ma50 = _sma(closes, 50)
    ma200 = _sma(closes, 200)
    slope20 = _slope(closes, 10)
    rsi = _rsi(closes)

    window = bars[-30:]
    high = max(b["h"] for b in window)
    low = min(b["l"] for b in window)
    span = high - low
    position = round((price - low) / span * 100, 0) if span else None

    atr = _atr(bars)
    recent_volume = [b["v"] for b in bars[-20:]]
    average_volume = sum(recent_volume) / len(recent_volume) if recent_volume else None
    rel_volume = round(bars[-1]["v"] / average_volume, 2) if average_volume else None

    above20 = round((price - ma20) / ma20 * 100, 2) if ma20 else None
    above50 = round((price - ma50) / ma50 * 100, 2) if ma50 else None
    above200 = round((price - ma200) / ma200 * 100, 2) if ma200 else None

    return {
        "price": price,
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "slope20": slope20,
        "above20": above20,
        "above50": above50,
        "above200": above200,
        "rsi": rsi,
        "atr_pct": round(atr / price * 100, 2) if atr else None,
        "range_high": high,
        "range_low": low,
        "range_pos": position,
        "change_7": _change(closes, 7),
        "change_30": _change(closes, 30),
        "change_90": _change(closes, 90),
        "rel_volume": rel_volume,
    }


def _trend(a):
    """Up, down or flat, from the two averages and the direction of the faster
    one. This names what the bars did; it does not claim what they will do."""
    up = a["ma20"] and a["ma50"] and a["price"] > a["ma20"] > a["ma50"]
    down = a["ma20"] and a["ma50"] and a["price"] < a["ma20"] < a["ma50"]
    rising = a["slope20"] is not None and a["slope20"] > 0.5
    falling = a["slope20"] is not None and a["slope20"] < -0.5
    if up and rising:
        return "uptrend"
    if down and falling:
        return "downtrend"
    if up or (rising and a["price"] > (a["ma20"] or a["price"])):
        return "leaning up"
    if down or (falling and a["price"] < (a["ma20"] or a["price"])):
        return "leaning down"
    return "sideways"


def _condition(rsi):
    if rsi is None:
        return None
    if rsi >= 70:
        return "overbought"
    if rsi <= 30:
        return "oversold"
    return "neutral"


# Read by the equity tool, which reuses this arithmetic on stock candles rather
# than keeping a second copy of it. The private names stay: the checks above
# call them by name, and a rename would be churn for nothing.
analyse = _analyse
trend_of = _trend
condition_of = _condition
ma_series = _ma_series
change_of = _change


def _render(base, quote, a, bars, source):
    trend = _trend(a)
    condition = _condition(a["rsi"])
    head = f"{base}/{quote} daily, {len(bars)} bars, {trend}"
    if condition and condition != "neutral":
        head += f", RSI {a['rsi']} ({condition})"

    parts = []
    if a["ma20"] is not None and a["above20"] is not None:
        side = "above" if a["above20"] >= 0 else "below"
        line = f"MA20 {crypto.money(a['ma20'])} ({a['above20']:+.2f}% {side})"
        if a["slope20"] is not None:
            line += f", MA20 slope {a['slope20']:+.2f}% over 10 days"
        parts.append(line)
    if a["ma50"] is not None and a["above50"] is not None:
        side = "above" if a["above50"] >= 0 else "below"
        parts.append(f"MA50 {crypto.money(a['ma50'])} ({a['above50']:+.2f}% {side})")
    if a["ma200"] is not None and a["above200"] is not None:
        side = "above" if a["above200"] >= 0 else "below"
        parts.append(f"MA200 {crypto.money(a['ma200'])} ({a['above200']:+.2f}% {side})")

    changes = [
        f"{label} {a[key]:+.2f}%"
        for key, label in (("change_7", "7d"), ("change_30", "30d"), ("change_90", "90d"))
        if a[key] is not None
    ]
    if changes:
        parts.append(", ".join(changes))

    if a["range_pos"] is not None:
        parts.append(
            f"30d range {crypto.money(a['range_low'])}–{crypto.money(a['range_high'])}, "
            f"price at {a['range_pos']:.0f}% of it"
        )
    if a["atr_pct"] is not None:
        parts.append(f"ATR14 {a['atr_pct']:.2f}% of price")
    if a["rel_volume"] is not None:
        parts.append(f"volume {a['rel_volume']:.2f}× its 20-day average")

    lines = [head + f". Last close {crypto.money(a['price'])} {quote}."]
    if parts:
        lines.append("; ".join(parts) + ".")
    lines.append(
        "These are measurements of past closes, not a forecast. Say so if the "
        "answer reads as a prediction. The reader sees the candles themselves, "
        "so name a couple of closing prices rather than describing every bar."
    )
    lines.append(f"Source: {source} daily candles, as of {crypto.when(int(time.time()))}.")
    return "\n".join(lines)[:MAX_CHARS]


def _trend_card(base, quote, a, bars, source, url):
    closes = [b["c"] for b in bars]
    return {
        "symbol": base,
        "quote": quote,
        "verdict": _trend(a),
        "condition": _condition(a["rsi"]),
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
        "source": source,
        "url": url,
        "at": int(time.time()),
        # The same bars the verdict was read from, so the chart under it can
        # never show a different window than the sentence above it.
        "interval": "1d",
        "candles": [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars],
        # The averages are computed once, here. The browser draws these numbers
        # rather than recomputing them, so the line and the facts agree.
        "ma20_series": _ma_series(closes, 20),
        "ma50_series": _ma_series(closes, 50),
        "chart_high": max(b["h"] for b in bars),
        "chart_low": min(b["l"] for b in bars),
        "change_window": _change(closes, len(closes) - 1),
    }


async def _load(args):
    """`(base, quote, bars, source, url, days)` or a failure tuple."""
    args = as_args(args)
    base, quote = crypto.parse_quote(args)
    if not base:
        return False, "No coin given.", "no coin was given", None
    days = _days(args)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            bars, source, url, _ = await _gather(client, base, quote, days)
    except crypto.Unavailable as e:
        if e.limited:
            text = "Every candle source is rate-limiting right now. Try again in a minute."
            return False, text, "every candle source is rate-limiting", None
        text = f"No candle source lists {base} against {quote}."
        return False, text, e.reason, None
    if len(bars) < MIN_BARS:
        text = (f"Only {len(bars)} daily bars came back for {base}/{quote}, too few to "
                f"read a trend. {MIN_BARS} are needed.")
        return False, text, "not enough daily history", None
    return True, "", "", (base, quote, bars, source, url)


async def read_trend(args):
    """`(ok, text, reason, meta)` for the trend tool."""
    ok, text, reason, loaded = await _load(args)
    if not ok:
        return False, text, reason, {}
    base, quote, bars, source, url = loaded
    a = _analyse(bars)
    source_row = {
        "source": url,
        "title": f"{base}/{quote} daily — {source}",
        "kind": "web",
        "read": False,
    }
    return (
        True,
        _render(base, quote, a, bars, source),
        "",
        {"sources": [source_row], "card": _trend_card(base, quote, a, bars, source, url)},
    )


TREND_TOOL = {
    "type": "function",
    "function": {
        "name": "crypto_trend",
        "description": (
            "Read the daily trend of a cryptocurrency and draw its candles: "
            "moving averages, RSI, volatility, where price sits in its 30-day "
            "range, and the move over 7, 30 and 90 days, with the same bars shown "
            "as a chart. Use it when asked whether a coin is bullish, bearish, "
            "trending or stretched, or when a picture of the price would answer "
            "better than a sentence. Read-only. It describes past closes and "
            "does not forecast."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string",
                           "description": "The coin: 'BTC', 'ETH', or 'BTC/USDT'."},
                "quote": {"type": "string",
                          "description": "The currency to price it in. Default USD."},
                "days": {"type": "integer",
                         "description": f"Daily bars to read, {MIN_DAYS} to {MAX_DAYS}. "
                                        f"Default {DEFAULT_DAYS}."},
            },
            "required": ["symbol"],
        },
    },
}


async def run_trend(args):
    """Handler for the trend tool. Returns text for the model, never raises."""
    return (await read_trend(args))[1]
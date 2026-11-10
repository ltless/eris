"""What the price tools share: the formatters, the candle arithmetic, and the
symbol grammar they read a question with.

crypto, market and equity read the same numbers back to a reader, in the same
units, so the formatting, the indicator maths and the coin vocabulary live here
rather than in whichever tool needed them first. Each tool keeps its own
sources and its own word for what a bar is; none of them keeps a second copy of
`money` or a second table of coin names.
"""

import time

# The fewest bars `analyse` needs before its long averages mean anything.
MIN_BARS = 20

# What a question is priced in when it names no currency of its own.
DEFAULT_QUOTE = "USD"

# Binance quotes in stablecoins where the others quote in the fiat itself.
_BINANCE_QUOTE = {"USD": "USDT"}

# The coins a question most often names outright. Anything else is read as a
# ticker, which is what the two exchange sources take anyway.
COIN_NAMES = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "ether": "ETH",
    "solana": "SOL",
    "cardano": "ADA",
    "dogecoin": "DOGE",
    "ripple": "XRP",
    "polkadot": "DOT",
    "chainlink": "LINK",
    "litecoin": "LTC",
    "tron": "TRX",
    "avalanche": "AVAX",
    "polygon": "MATIC",
    "monero": "XMR",
    "binancecoin": "BNB",
    "bnb": "BNB",
    "tether": "USDT",
    "usd-coin": "USDC",
    "shiba-inu": "SHIB",
    "pepe": "PEPE",
}


def parse_symbol(value):
    """`(base, quote)` from what the model sent, or `(None, quote)`.

    A pair may arrive as `BTC/USDT`, `BTC-USDT`, or the two fields separately.
    """
    text = str(value or "").strip().upper().replace("-", "/")
    if not text:
        return None, DEFAULT_QUOTE
    head, _, tail = text.partition("/")
    head = head.strip()
    return (COIN_NAMES.get(head.lower(), head) or None), (tail.strip() or DEFAULT_QUOTE)


def parse_quote(args):
    sent = str(args.get("quote") or "").strip().upper()
    base, quoted = parse_symbol(args.get("symbol"))
    return base, sent or quoted


def binance_pair(base, quote):
    """The symbol Binance lists this coin under."""
    return base + _BINANCE_QUOTE.get(quote, quote)


def coinbase_pair(base, quote):
    """The product id Coinbase lists this coin under."""
    return f"{base}-{quote}"


class Unavailable(Exception):
    """One source could not answer. `limited` tells a rate limit apart from a
    source that simply does not list the symbol."""

    def __init__(self, reason, limited=False):
        super().__init__(reason)
        self.reason = reason
        self.limited = limited


def no_source(limited, kind, pair):
    """The last word when every source refused, as `(text, reason)`.

    A limit everywhere is a wait, anything else is the pair not being listed,
    so the tool that asked decides nothing beyond which of the two it heard.
    """
    if limited:
        return (
            f"Every {kind} source is rate-limiting right now. Try again in a minute.",
            f"every {kind} source is rate-limiting",
        )
    return f"No {kind} source lists {pair}.", f"no {kind} source lists {pair}"


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pos(value):
    """A figure, or `None` when there was nothing to report.

    A source with no trading in the window answers zero for the high, the low
    and the volume. Printed as a price, that zero is a wrong fact; dropped, the
    row says it has nothing, which is true.
    """
    return value or None


def money(value):
    if value is None:
        return "?"
    if value >= 1:
        return f"{value:,.2f}"
    if value >= 0.01:
        return f"{value:.4f}"
    return f"{value:.8f}"


TIERS = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"))


def big(value):
    if value is None:
        return "?"
    for cut, suffix in TIERS:
        if value >= cut:
            return f"{value / cut:,.2f}{suffix}"
    return f"{value:,.2f}"


def when(epoch):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))


# Indicators read off a close series. Wilder's smoothing is not used: the simple
# average is what the card states, and a first-value difference is not worth a
# second formula to test.
def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ma_series(closes, n):
    out = []
    total = 0.0
    for i, close in enumerate(closes):
        total += close
        if i >= n:
            total -= closes[i - n]
        out.append(round(total / n, 8) if i >= n - 1 else None)
    return out


def rsi(closes, n=14):
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


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    ranges = []
    for prev, cur in zip(bars[-n - 1:-1], bars[-n:]):
        ranges.append(max(cur["h"] - cur["l"],
                          abs(cur["h"] - prev["c"]),
                          abs(cur["l"] - prev["c"])))
    return sum(ranges) / n


def slope(values, n):
    """Percent change of the last two `n`-bar averages, or None."""
    if len(values) < n * 2:
        return None
    now = sum(values[-n:]) / n
    before = sum(values[-2 * n:-n]) / n
    if not before:
        return None
    return round((now - before) / before * 100, 2)


def change(closes, n):
    if len(closes) <= n or not closes[-n - 1]:
        return None
    return round((closes[-1] - closes[-n - 1]) / closes[-n - 1] * 100, 2)


def analyse(bars):
    """Every figure the trend card and the model's sentence are built from."""
    closes = [b["c"] for b in bars]
    price = closes[-1]
    ma20 = sma(closes, 20)
    ma50 = sma(closes, 50)
    ma200 = sma(closes, 200)
    slope20 = slope(closes, 10)
    rsi14 = rsi(closes)

    window = bars[-30:]
    high = max(b["h"] for b in window)
    low = min(b["l"] for b in window)
    span = high - low
    position = round((price - low) / span * 100, 0) if span else None

    atr14 = atr(bars)
    recent_volume = [b["v"] for b in bars[-20:]]
    average_volume = sum(recent_volume) / len(recent_volume) if recent_volume else None
    rel_volume = round(bars[-1]["v"] / average_volume, 2) if average_volume else None

    return {
        "price": price,
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "slope20": slope20,
        "above20": round((price - ma20) / ma20 * 100, 2) if ma20 else None,
        "above50": round((price - ma50) / ma50 * 100, 2) if ma50 else None,
        "above200": round((price - ma200) / ma200 * 100, 2) if ma200 else None,
        "rsi": rsi14,
        "atr_pct": round(atr14 / price * 100, 2) if atr14 else None,
        "range_high": high,
        "range_low": low,
        "range_pos": position,
        "change_7": change(closes, 7),
        "change_30": change(closes, 30),
        "change_90": change(closes, 90),
        "rel_volume": rel_volume,
    }


def trend_of(a):
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


def condition_of(rsi_value):
    if rsi_value is None:
        return None
    if rsi_value >= 70:
        return "overbought"
    if rsi_value <= 30:
        return "oversold"
    return "neutral"


def trend_prose(subject, a, *, close, source, unit="day"):
    """The model's sentence for a trend, from what `analyse` returned.

    `subject` names the instrument and the window, `close` states the last price
    with its currency, and `source` names where the bars came from. The figures
    between them read the same for a coin and for a share, so they are written
    here once; only the noun for a bar differs, and a caller that charts
    something other than days passes `unit="bar"`.
    """
    head = f"{subject}, {trend_of(a)}"
    condition = condition_of(a["rsi"])
    if condition and condition != "neutral":
        head += f", RSI {a['rsi']} ({condition})"

    parts = []
    for n in (20, 50, 200):
        level, above = a[f"ma{n}"], a[f"above{n}"]
        if level is None or above is None:
            continue
        side = "above" if above >= 0 else "below"
        line = f"MA{n} {money(level)} ({above:+.2f}% {side})"
        if n == 20 and a["slope20"] is not None:
            line += f", MA20 slope {a['slope20']:+.2f}% over 10 {unit}s"
        parts.append(line)

    suffix = "d" if unit == "day" else " bars"
    changes = [
        f"{label} {a[key]:+.2f}%"
        for key, label in (("change_7", f"7{suffix}"), ("change_30", f"30{suffix}"),
                           ("change_90", f"90{suffix}"))
        if a[key] is not None
    ]
    if changes:
        parts.append(", ".join(changes))

    if a["range_pos"] is not None:
        parts.append(f"30-{unit} range {money(a['range_low'])}–{money(a['range_high'])}, "
                     f"price at {a['range_pos']:.0f}% of it")
    if a["atr_pct"] is not None:
        parts.append(f"ATR14 {a['atr_pct']:.2f}% of price")
    if a["rel_volume"] is not None:
        parts.append(f"volume {a['rel_volume']:.2f}× its 20-{unit} average")

    lines = [head + f". Last close {close}."]
    if parts:
        lines.append("; ".join(parts) + ".")
    lines.append(
        "These are measurements of past closes, not a forecast. Say so if the "
        "answer reads as a prediction. The reader sees the candles themselves, "
        "so name a couple of closing prices rather than describing every bar."
    )
    lines.append(f"Source: {source}, as of {when(int(time.time()))}.")
    return "\n".join(lines)


def trend_card(a, bars, *, symbol, quote, source, url, interval="1d",
               name=None, exchange=None):
    """The trend card's payload, field for field, from the figures and the bars.

    Both price tools draw the same chart, so the card is built once here rather
    than twice in two modules that would then be free to drift apart. `name` and
    `exchange` belong to a listing and are absent for a coin.

    Every figure the card states is already in `a`, so they are spread in rather
    than listed again: a figure added to the analysis reaches the card without a
    second edit, and a list here could only fall behind it.
    """
    closes = [b["c"] for b in bars]
    card = {
        # `a` first, so a figure can never shadow one of the keys stated below.
        **a,
        "symbol": symbol,
        "quote": quote,
        "verdict": trend_of(a),
        "condition": condition_of(a["rsi"]),
        "bar_count": len(bars),
        "interval": interval,
        "source": source,
        "url": url,
        "at": int(time.time()),
        # The same bars the verdict was read from, so the chart under it can
        # never show a different window than the sentence above it.
        "candles": [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars],
        # The averages are computed once, here. The browser draws these numbers
        # rather than recomputing them, so the line and the facts agree.
        "ma20_series": ma_series(closes, 20),
        "ma50_series": ma_series(closes, 50),
        "chart_high": max(b["h"] for b in bars),
        "chart_low": min(b["l"] for b in bars),
    }
    if name is not None:
        card["name"] = name
    if exchange is not None:
        card["exchange"] = exchange
    return card

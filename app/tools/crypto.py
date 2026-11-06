"""Crypto prices, read-only, from public sources tried in turn.

Binance, Coinbase and CoinGecko all answer without a key, and each is rate
limited on its own schedule, so a call asks them in an order that fits the
quote currency and takes the first answer. Nothing here can place an order:
the tool reads market data and no source is asked for anything else.
"""

import time

import httpx

from . import as_args

BINANCE_URL = "https://api.binance.com/api/v3/ticker/24hr"
COINBASE_URL = "https://api.exchange.coinbase.com/products/{pair}/stats"
GECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"
TIMEOUT = 10
MAX_CHARS = 3_000
DEFAULT_QUOTE = "USD"

# Binance quotes in stablecoins where the others quote in the fiat itself.
BINANCE_QUOTE = {"USD": "USDT"}

# The coins a question most often names outright. Anything else is read as a
# ticker, which is what the two exchange sources take anyway.
NAMES = {
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

TOOL = {
    "type": "function",
    "function": {
        "name": "crypto",
        "description": (
            "Current price and 24-hour move for a cryptocurrency. Use it for "
            "any question about how much a coin costs or how it moved today, "
            "instead of searching for it. Read-only: it cannot place, cancel, "
            "or inspect anyone's orders."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": (
                        "The coin, as a ticker or a name: 'BTC', 'ETH', 'bitcoin', "
                        "or a pair like 'BTC/USDT'."
                    ),
                },
                "quote": {
                    "type": "string",
                    "description": f"The currency to price it in. Default {DEFAULT_QUOTE}.",
                },
            },
            "required": ["symbol"],
        },
    },
}


class Unavailable(Exception):
    """One source could not answer. `limited` tells a rate limit apart from a
    source that simply does not list the coin."""

    def __init__(self, reason, limited=False):
        super().__init__(reason)
        self.reason = reason
        self.limited = limited


def parse_symbol(value):
    """`(base, quote)` from what the model sent, or `(None, quote)`.

    A pair may arrive as `BTC/USDT`, `BTC-USDT`, or the two fields separately.
    """
    text = str(value or "").strip().upper().replace("-", "/")
    if not text:
        return None, DEFAULT_QUOTE
    head, _, tail = text.partition("/")
    head = head.strip()
    return (NAMES.get(head.lower(), head) or None), (tail.strip() or DEFAULT_QUOTE)


def parse_quote(args):
    sent = str(args.get("quote") or "").strip().upper()
    base, quoted = parse_symbol(args.get("symbol"))
    return base, sent or quoted


async def _binance(c, base, quote):
    pair = base + BINANCE_QUOTE.get(quote, quote)
    r = await c.get(BINANCE_URL, params={"symbol": pair})
    if r.status_code in (418, 429):
        raise Unavailable("binance is rate-limiting", limited=True)
    if r.status_code == 400:
        raise Unavailable("binance has no such pair")
    r.raise_for_status()
    d = r.json()
    return {
        "source": "Binance",
        "url": BINANCE_URL,
        "pair": pair,
        "price": _num(d.get("lastPrice")),
        "change": _num(d.get("priceChangePercent")),
        "high": _pos(_num(d.get("highPrice"))),
        "low": _pos(_num(d.get("lowPrice"))),
        "volume": _pos(_num(d.get("quoteVolume"))),
        "volume_scope": "this pair on Binance",
        "name": None,
        "market_cap": None,
        "rank": None,
    }


async def _coinbase(c, base, quote):
    pair = f"{base}-{quote}"
    r = await c.get(COINBASE_URL.format(pair=pair))
    if r.status_code == 429:
        raise Unavailable("coinbase is rate-limiting", limited=True)
    if r.status_code == 404:
        raise Unavailable("coinbase has no such pair")
    r.raise_for_status()
    d = r.json()
    last = _num(d.get("last"))
    opened = _num(d.get("open"))
    # `open`, `high` and `low` track the rolling 24 hours, matching the candle
    # history, so the move is a true 24-hour one. `volume` is in the base coin,
    # not the quote, so it is converted before it can sit next to the others.
    change = round((last - opened) / opened * 100, 2) if last and opened else None
    volume = _num(d.get("volume"))
    return {
        "source": "Coinbase",
        "url": f"https://www.coinbase.com/price/{base.lower()}",
        "pair": pair,
        "price": last,
        "change": change,
        "high": _pos(_num(d.get("high"))),
        "low": _pos(_num(d.get("low"))),
        "volume": _pos(volume * last) if volume and last else None,
        "volume_scope": "this pair on Coinbase",
        "name": None,
        "market_cap": None,
        "rank": None,
    }


async def _gecko(c, base, quote):
    r = await c.get(
        GECKO_URL,
        params={"vs_currency": quote.lower(), "symbols": base.lower()},
    )
    if r.status_code == 429:
        raise Unavailable("coingecko is rate-limiting", limited=True)
    if r.status_code == 400:
        raise Unavailable("coingecko does not price in that currency")
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise Unavailable("coingecko has no such coin")
    d = rows[0]
    return {
        "source": "CoinGecko",
        "url": f"https://www.coingecko.com/en/coins/{d.get('id') or base.lower()}",
        "pair": f"{base}/{quote}",
        "price": _num(d.get("current_price")),
        "change": _num(d.get("price_change_percentage_24h")),
        "high": _pos(_num(d.get("high_24h"))),
        "low": _pos(_num(d.get("low_24h"))),
        "volume": _pos(_num(d.get("total_volume"))),
        "volume_scope": "every market CoinGecko tracks",
        "name": d.get("name") or None,
        "market_cap": _pos(_num(d.get("market_cap"))),
        "rank": d.get("market_cap_rank"),
    }


def _sources(quote):
    """The order to ask in. The exchanges answer one pair at a time and are the
    faster of the three; CoinGecko is last because its free tier rate-limits
    hardest, and it is also the only one that names the coin and its cap, so a
    call that reaches it trades speed for a fuller card."""
    if quote in ("USD", "USDT", "USDC"):
        return (_binance, _coinbase, _gecko)
    return (_coinbase, _binance, _gecko)


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pos(value):
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


def _big(value):
    if value is None:
        return "?"
    for cut, suffix in TIERS:
        if value >= cut:
            return f"{value / cut:,.2f}{suffix}"
    return f"{value:,.2f}"


# The equity card states volume and market cap the same way, so the formatter is
# shared rather than copied. The private name stays for the callers above.
big = _big


def when(epoch):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))


def _render(base, quote, price):
    name = f"{price['name']} ({base})" if price.get("name") else base
    lines = [
        f"{name} is {money(price['price'])} {quote}"
        + (f", {price['change']:+.2f}% over 24 hours." if price.get("change") is not None else ".")
    ]
    figures = []
    if price.get("high") is not None:
        figures.append(f"24h high {money(price['high'])}")
    if price.get("low") is not None:
        figures.append(f"24h low {money(price['low'])}")
    if price.get("volume") is not None:
        scope = price.get("volume_scope")
        figures.append(
            f"24h volume {_big(price['volume'])} {quote}"
            + (f" ({scope})" if scope else "")
        )
    if price.get("market_cap") is not None:
        cap = f"market cap {_big(price['market_cap'])} {quote}"
        if price.get("rank"):
            cap += f" (rank {price['rank']})"
        figures.append(cap)
    if figures:
        lines.append(", ".join(figures) + ".")
    lines.append(f"Source: {price['source']}, as of {when(price['at'])}.")
    return "\n".join(lines)[:MAX_CHARS]


def _card(base, quote, price):
    """The card's own figures. The model reads `_render`; the reader sees this."""
    return {
        "symbol": base,
        "quote": quote,
        "name": price.get("name") or base,
        "price": price.get("price"),
        "change": price.get("change"),
        "high": price.get("high"),
        "low": price.get("low"),
        "volume": price.get("volume"),
        "volume_scope": price.get("volume_scope"),
        "market_cap": price.get("market_cap"),
        "rank": price.get("rank"),
        "source": price["source"],
        "url": price["url"],
        "at": price["at"],
    }


async def read(args):
    """`(ok, text, reason, meta)`. `reason` names the failure alone; `meta`
    carries the source list and the figures the card draws."""
    args = as_args(args)
    base, quote = parse_quote(args)
    if not base:
        return False, "No coin given.", "no coin was given", {}

    failures = []
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        for ask in _sources(quote):
            try:
                price = await ask(client, base, quote)
            except Unavailable as e:
                failures.append(e)
                continue
            except httpx.HTTPError as e:
                failures.append(Unavailable(f"{type(e).__name__}"))
                continue
            except ValueError:
                failures.append(Unavailable("an unreadable reply"))
                continue
            if price.get("price") is None:
                failures.append(Unavailable("no price in the reply"))
                continue
            price["at"] = int(time.time())
            source = {
                "source": price["url"],
                "title": f"{base}/{quote} — {price['source']}",
                "kind": "web",
                "read": False,
            }
            return (
                True,
                _render(base, quote, price),
                "",
                {"sources": [source], "card": _card(base, quote, price)},
            )

    # Every source was asked and none answered. What they said decides which
    # sentence fits: a rate limit is a wait, an unknown coin is a correction.
    if failures and all(f.limited for f in failures):
        text = "Every price source is rate-limiting right now. Try again in a minute."
        return False, text, "every price source is rate-limiting", {}
    text = f"No price source lists {base} against {quote}."
    return False, text, f"no price source lists {base} against {quote}", {}


async def run(args):
    """Handler for the crypto tool. Returns text for the model, never raises."""
    return (await read(args))[1]
"""Crypto prices, read-only, from public sources tried in turn.

Binance, Coinbase and CoinGecko all answer without a key, and each is rate
limited on its own schedule, so a call asks them in an order that fits the
quote currency and takes the first answer. Nothing here can place an order:
the tool reads market data and no source is asked for anything else.
"""

import time

import httpx

from ..errors import TOOL_ERROR
from ..security import EXTERNAL
from .marketdata import (
    DEFAULT_QUOTE, Unavailable, big, binance_pair, coinbase_pair, money, no_source, num,
    parse_quote, pos, when,
)
from .registry import TAGS, Tool, ToolResult, missing_arg, registry, source_row

BINANCE_URL = "https://api.binance.com/api/v3/ticker/24hr"
COINBASE_URL = "https://api.exchange.coinbase.com/products/{pair}/stats"
GECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"
TIMEOUT = 10
MAX_CHARS = 3_000
NAME = "crypto"
TIMEOUT_S = 35.0

DESCRIPTION = (
    "Current price and 24-hour move for a cryptocurrency. Use it for "
    "any question about how much a coin costs or how it moved today, "
    "instead of searching for it. Read-only: it cannot place, cancel, "
    "or inspect anyone's orders."
)

PARAMETERS = {
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
}


async def _binance(c, base, quote):
    pair = binance_pair(base, quote)
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
        "price": num(d.get("lastPrice")),
        "change": num(d.get("priceChangePercent")),
        "high": pos(num(d.get("highPrice"))),
        "low": pos(num(d.get("lowPrice"))),
        "volume": pos(num(d.get("quoteVolume"))),
        "volume_scope": "this pair on Binance",
        "name": None,
        "market_cap": None,
        "rank": None,
    }


async def _coinbase(c, base, quote):
    pair = coinbase_pair(base, quote)
    r = await c.get(COINBASE_URL.format(pair=pair))
    if r.status_code == 429:
        raise Unavailable("coinbase is rate-limiting", limited=True)
    if r.status_code == 404:
        raise Unavailable("coinbase has no such pair")
    r.raise_for_status()
    d = r.json()
    last = num(d.get("last"))
    opened = num(d.get("open"))
    # `open`, `high` and `low` track the rolling 24 hours, matching the candle
    # history, so the move is a true 24-hour one. `volume` is in the base coin,
    # not the quote, so it is converted before it can sit next to the others.
    change = round((last - opened) / opened * 100, 2) if last and opened else None
    volume = num(d.get("volume"))
    return {
        "source": "Coinbase",
        "url": f"https://www.coinbase.com/price/{base.lower()}",
        "price": last,
        "change": change,
        "high": pos(num(d.get("high"))),
        "low": pos(num(d.get("low"))),
        "volume": pos(volume * last) if volume and last else None,
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
        "price": num(d.get("current_price")),
        "change": num(d.get("price_change_percentage_24h")),
        "high": pos(num(d.get("high_24h"))),
        "low": pos(num(d.get("low_24h"))),
        "volume": pos(num(d.get("total_volume"))),
        "volume_scope": "every market CoinGecko tracks",
        "name": d.get("name") or None,
        "market_cap": pos(num(d.get("market_cap"))),
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
            f"24h volume {big(price['volume'])} {quote}"
            + (f" ({scope})" if scope else "")
        )
    if price.get("market_cap") is not None:
        cap = f"market cap {big(price['market_cap'])} {quote}"
        if price.get("rank"):
            cap += f" (rank {price['rank']})"
        figures.append(cap)
    if figures:
        lines.append(", ".join(figures) + ".")
    lines.append(f"Source: {price['source']}, as of {when(price['at'])}.")
    return "\n".join(lines)[:MAX_CHARS]


def _card(base, quote, price):
    """The card's own figures. The model reads `_render`; the reader sees this.

    `rank` is not here: the card draws the market cap without it, and the model
    reads the rank where it is written into the rendered text.
    """
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
        "source": price["source"],
        "url": price["url"],
        "at": price["at"],
    }


async def read(args):
    """Answer the crypto tool, or say which failure stopped it."""
    base, quote = parse_quote(args)
    if not base:
        return missing_arg("coin")

    failures = []
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        for ask in _sources(quote):
            try:
                price = await ask(client, base, quote)
            except Unavailable as e:
                failures.append(e)
                continue
            except httpx.HTTPError as e:
                failures.append(Unavailable(
                    f"{type(e).__name__}",
                    unreached=isinstance(e, httpx.TransportError),
                ))
                continue
            except ValueError:
                failures.append(Unavailable("an unreadable reply"))
                continue
            if price.get("price") is None:
                failures.append(Unavailable("no price in the reply"))
                continue
            price["at"] = int(time.time())
            source = source_row(price["url"], f"{base}/{quote} — {price['source']}")
            return ToolResult.ok(
                _render(base, quote, price),
                sources=[source],
                card=_card(base, quote, price),
            )

    # Every source was asked and none answered. What they said decides which
    # sentence fits: a rate limit is a wait, an unknown coin is a correction.
    text, reason = no_source(
        bool(failures) and all(f.limited for f in failures), "price", f"{base} against {quote}",
        unreached=bool(failures) and all(f.unreached for f in failures),
    )
    return ToolResult.failed(TOOL_ERROR, text, reason=reason)


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

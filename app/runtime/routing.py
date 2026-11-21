"""Select tool candidates from the user's request and recent context."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    tools: tuple[str, ...] = ()
    fallback: tuple[str, ...] = ()
    needs_data: bool = False
    after_data: tuple[str, ...] = ()
    news_query: str | None = None
    news_hint: str = ""


_URL = re.compile(r"https?://\S+")
_FOLLOW_UP = re.compile(r"^(?:kalau|kalo|klo|gimana dengan|bagaimana dengan|yang tadi|dan|what about|and)\b")
_GREETING = re.compile(r"(?:halo|hai|hi|hey|hello|makasih|terima kasih|thanks|thank you)[\s!.?]*$")
_CONCEPT = re.compile(
    r"^(?:apa itu|apa bedanya|jelasin|jelaskan|bagaimana cara|how does|how to|what is|what are|"
    r"explain|terjemahkan|translate|parafrase|paraphrase|rewrite)\b"
)
_FRESH = re.compile(r"\b(?:terbaru|terkini|sekarang|hari ini|minggu ini|besok|latest|current|today|tonight|tomorrow|right now|this week)\b")
_SEARCH = re.compile(r"\b(?:cari(?:kan)?|telusuri|berita|sumber|search|look up|find|news|sources)\b")
_NEWS = re.compile(r"\b(?:berita|kabar|news|headlines|pemberitaan)\b", re.IGNORECASE)
_NEWS_LOOKUP = re.compile(r"\b(?:cari(?:kan)?|cek|lihat|telusuri|ambil|search|find|show|look up|check)\b")
_NEWS_DECLINED = re.compile(
    r"\b(?:jangan|tanpa|(?:tidak|nggak|gak) perlu|don['’]t|do not|without|no need to)\s+"
    rf"(?:{_NEWS_LOOKUP.pattern}\s+)?(?:(?:the|latest|recent)\s+)?{_NEWS.pattern}"
)
_TICKER = re.compile(r"\b[A-Z0-9]{1,10}\.[A-Z]{1,4}\b", re.IGNORECASE)
_NEWS_TOPIC_PREFIX = re.compile(
    r"^(?:(?:terbaru|terkini|recent|latest|today|hari ini|tentang|soal|terkait|mengenai|about|on|for)\b"
    r"[\s:]+)+", re.IGNORECASE,
)
_CLAUSE_END = re.compile(r"[!?;\n]|\.\s|,\s|\b(?:lalu|kemudian|then|dan\s+(?:ringkas|jelaskan|berikan))\b",
                         re.IGNORECASE)
_WEATHER = re.compile(r"\b(?:cuaca|suhu|hujan|prakiraan|weather|temperature|rain|forecast)\b")
_CRYPTO = re.compile(r"\b(?:kripto|crypto|bitcoin|ethereum|btc|eth|solana|dogecoin|usdt)\b")
_EQUITY = re.compile(r"\b(?:saham|bursa|ihsg|etf|stock|stocks|equity|ticker|bbca|bbri|aapl|nvda|nasdaq|s&p 500)\b")
_MARKET = re.compile(r"\b(?:harga|price|quote|trend|tren|grafik|chart|pergerakan|historis|historical)\b")
_TREND = re.compile(r"\b(?:trend|tren|grafik|chart|pergerakan|historis|historical|minggu ini|sebulan|this week|past week)\b")
_TECHNICAL = re.compile(r"\b(?:(?:s?ma|rsi|atr)\s*\d{1,3}|moving average|rata[- ]rata\s+\d+)\b")
_SCREEN = re.compile(r"\b(?:screener|screen saham|top gainers|top losers|paling naik|paling turun)\b")
_FILE = re.compile(r"\b(?:lampiran|attachment|attached|dokumen|document|file|berkas|workspace|folder|directory)\b")
_PATH = re.compile(r"\b[^\s/]+\.(?:txt|md|csv|tsv|json|yaml|yml|pdf|py|js|html|css)\b")
_WRITE = re.compile(r"\b(?:tulis(?:kan)?|buat(?:kan)?|simpan|write|create|save|overwrite|timpa)\b")
_READ = re.compile(r"\b(?:baca|ringkas|read|summarize|review)\b")
_LIST = re.compile(r"\b(?:daftar|list|lihat isi)\b")
_ARCHIVE = re.compile(r"\b(?:wayback|arsip|archive|snapshot|versi lama|old version)\b")
_DEPENDENCY = re.compile(r"\b(?:lalu|kemudian|setelah itu|then|after that)\b")
_DATA_TOOLS = frozenset({
    "web_search", "fetch_url", "wayback", "weather", "crypto", "crypto_trend",
    "equity", "equity_trend", "screen",
})


def select_route(query, *, previous="", has_attachments=False):
    current = query.casefold().strip()
    if not has_attachments and _GREETING.fullmatch(current):
        return Route()

    concepts = _CONCEPT.search(current)
    explicit_lookup = _FRESH.search(current) or _SEARCH.search(current) or _MARKET.search(current)
    link = bool(_URL.search(current))
    file_request = bool(_FILE.search(current) or _PATH.search(current))
    if concepts and not (explicit_lookup or link or file_request or has_attachments):
        return Route()

    scope = current
    if len(current) <= 100 and _FOLLOW_UP.match(current):
        scope = f"{current}\n{previous.casefold()[:500]}"
    news = None
    if (_NEWS.search(current) and not _NEWS_DECLINED.search(current)
            and (_FRESH.search(current) or _NEWS_LOOKUP.search(current) or _NEWS.match(current))):
        news = _news_query(query)
    if (news is None and scope != current and _NEWS.search(previous) and _FRESH.search(current)
            and not _NEWS_DECLINED.search(previous.casefold()) and not _NEWS_DECLINED.search(current)):
        news = _news_query(previous)
    archived = _ARCHIVE.search(scope) and (_URL.search(scope) or _SEARCH.search(scope)
                                          or "wayback" in scope)
    weather = _WEATHER.search(scope)
    trend = _TREND.search(scope) or _TECHNICAL.search(scope)
    market_request = _MARKET.search(scope) or trend or _SCREEN.search(scope)
    market_lookup = market_request or (news is None and (explicit_lookup or _FRESH.search(scope)))

    tools = []
    fallback = []
    if archived:
        tools.append("wayback")
        fallback.append("web_search")
    elif _URL.search(scope):
        tools.append("fetch_url")
        fallback.append("web_search")
    if weather:
        tools.append("weather")
        fallback.append("web_search")
    if market_lookup and _CRYPTO.search(scope):
        tools.append("crypto_trend" if trend else "crypto")
        fallback.append("web_search")
    if market_lookup and _EQUITY.search(scope):
        tools.append("screen" if _SCREEN.search(scope) else
                     "equity_trend" if trend else "equity")
        fallback.append("web_search")
    if has_attachments or _FILE.search(scope) or _PATH.search(scope):
        if file_request and _WRITE.search(current) and not concepts:
            if has_attachments or _READ.search(current):
                tools.append("read_file")
            tools.append("write_file")
        elif file_request and _LIST.search(current) and "lampiran" not in current:
            tools.append("list_workspace")
        else:
            tools.append("read_file")

    if news is not None or _SEARCH.search(scope) or (not tools and _FRESH.search(scope)):
        tools.append("web_search")
    tools = list(dict.fromkeys(tools))
    dependent = _DEPENDENCY.search(current)
    if len(tools) == 2 and dependent:
        tools.append("plan")

    if not tools:
        return Route(("web_search",))

    needs_data = bool(_DATA_TOOLS.intersection(tools))
    after_data = ()
    if dependent and needs_data and "write_file" in tools:
        tools.remove("write_file")
        after_data = ("write_file",)
    previous_ticker = _TICKER.search(previous) if news == "" else None
    hint = f"{previous_ticker[0]} latest news" if previous_ticker else " ".join(query.split())[:220]
    return Route(tuple(tools), tuple(dict.fromkeys(fallback)), needs_data, after_data, news,
                 hint if news == "" else "")


def _news_query(text):
    match = _NEWS.search(text)
    tail = text[match.end():].strip(" :,-")
    topic = _CLAUSE_END.split(_NEWS_TOPIC_PREFIX.sub("", tail), maxsplit=1)[0].strip(" .?,'\"")
    return f"{topic[:180]} latest news" if topic and not _FRESH.fullmatch(topic.casefold()) else ""


def missing_news_query(route, chart_symbol):
    if route.news_query:
        return route.news_query
    if chart_symbol:
        return f"{chart_symbol} latest news"
    return route.news_hint or "latest news"


def is_news_lookup(route, args):
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return False
    market = {"equity", "equity_trend", "crypto", "crypto_trend", "screen"}
    if route is None or not market.intersection(route.tools):
        return True
    if _NEWS.search(str(args.get("categories", ""))) or _NEWS.search(query):
        return True
    query = query.casefold()
    return bool(_FRESH.search(query) and not _TECHNICAL.search(query))


def provides_data(name, result):
    if name == "web_search":
        return bool((result.metadata or {}).get("sources"))
    return name in _DATA_TOOLS and bool(result.text and result.text.strip())


def links_from_search(name, result):
    return name == "web_search" and any(
        not source.get("read") and str(source.get("source", "")).startswith(("https://", "http://"))
        for source in (result.metadata or {}).get("sources", ())
    )

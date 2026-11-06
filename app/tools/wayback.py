"""Reading pages that are gone, from the Wayback Machine.

`fetch_url` stops at "the server answered 404". This looks the URL up in the
archive index, and reads the snapshot itself through the same fetcher, so a
dead link still gives up its text.
"""

import asyncio
import json
import re

import httpx

from ..config import settings
from . import as_args
from .fetch import HEADERS, fetch_page, is_public_url

CDX_URL = "https://web.archive.org/cdx/search/cdx"
ARCHIVE_URL = "https://web.archive.org/web/{stamp}id_/{url}"
TIMEOUT = 20
LIMIT = 5
DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")

TOOL = {
    "type": "function",
    "function": {
        "name": "wayback",
        "description": (
            "Read a URL from the Internet Archive, including pages that are "
            "gone or answering 404 now. Use it when fetch_url returned an error, "
            "or when the user asks for an old or deleted version of a page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to find in the archive, as it was.",
                },
                "date": {
                    "type": "string",
                    "description": (
                        "Ask for the snapshot nearest this date, as YYYY, "
                        "YYYY-MM, or YYYY-MM-DD. Omit for the newest one."
                    ),
                },
            },
            "required": ["url"],
        },
    },
}


def _window(date):
    """`(from, to)` CDX stamps for a partial date, or None if unreadable.

    CDX has no timestamp parameter: asking for 2019 without a window answers
    with rows from 2002.
    """
    m = DATE_RE.match(date.strip())
    if not m:
        return None
    year, month, day = m.group(1), m.group(2) or "01", m.group(3) or "01"
    if m.group(3):
        return f"{year}{month}{day}", f"{year}{month}{day}"
    if m.group(2):
        last = {"01": "31", "02": "29", "03": "31", "04": "30", "05": "31", "06": "30",
                "07": "31", "08": "31", "09": "30", "10": "31", "11": "30", "12": "31"}[month]
        return f"{year}{month}01", f"{year}{month}{last}"
    return f"{year}0101", f"{year}1231"


async def _snapshots(client, url, window):
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype",
        "filter": "statuscode:200",
        "collapse": "digest",
        "sort": "reverse",
        "limit": str(LIMIT),
    }
    if window:
        params["from"], params["to"] = window
    r = await client.get(CDX_URL, params=params)
    r.raise_for_status()
    body = r.text.strip()
    if not body or body == "[]":
        return []
    try:
        rows = json.loads(body)
    except json.JSONDecodeError:
        return []
    if not rows:
        return []
    head, *data = rows
    out = []
    for row in data:
        if not isinstance(row, list):
            continue
        record = dict(zip(head, row))
        if record.get("timestamp"):
            out.append(record)
    return out


def _when(row):
    stamp = str(row.get("timestamp", ""))
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 else stamp


def _row(row):
    """One snapshot as a card row: the day it was taken, the URL it was taken
    from, and the archived copy of it, which is what the row links to."""
    original = row.get("original", "")
    return {
        "when": _when(row),
        "url": original,
        "type": row.get("mimetype") or "",
        "read": ARCHIVE_URL.format(stamp=row.get("timestamp", ""), url=original),
    }


def _row_text(rows):
    lines = []
    for row in rows:
        lines.append(
            f"  {_when(row)}  {row.get('original', '')}  ({row.get('mimetype') or 'unknown'})"
        )
    return "\n".join(lines)


async def read(args):
    """`(ok, text, reason, meta)`. `reason` is empty on success, and comes back
    apart from the text so the caller can name the failure alone. `meta` is the
    tool result's metadata: the source list, and the snapshot the card shows."""
    args = as_args(args)
    url = str(args.get("url", "") or "").strip()
    if not url:
        return False, "No URL given.", "no URL was given", {}
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    if not await _to_public(url):
        return False, f"{url} is not a public http(s) address.", "not a public http(s) address", {}
    date = str(args.get("date", "") or "").strip()
    window = _window(date) if date else None
    if date and window is None:
        return (
            False,
            f"{date!r} is not a date this tool can read. Use YYYY, YYYY-MM, or YYYY-MM-DD.",
            "the date could not be read",
            {},
        )

    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=True, headers=HEADERS
        ) as client:
            rows = await _snapshots(client, url, window)
            note = ""
            if not rows and window:
                # Nothing in the window asked for. The archive may still hold
                # the page; it just never took it during those months.
                rows = await _snapshots(client, url, None)
                if rows:
                    note = f"Nothing archived in {date}; these are the snapshots it does hold.\n"
            if not rows:
                return (
                    False,
                    f"The Internet Archive holds no snapshot of {url}.",
                    "the archive holds no snapshot of that URL",
                    {},
                )
            chosen = rows[0]
            stamp = str(chosen["timestamp"])
            archived = ARCHIVE_URL.format(stamp=stamp, url=chosen.get("original") or url)
            text, reason = await fetch_page(client, archived, settings.fetch_max_chars)
    except httpx.HTTPError as e:
        return (
            False,
            f"The Internet Archive could not be reached ({type(e).__name__}).",
            "the archive could not be reached",
            {},
        )
    except ValueError:
        return (
            False,
            "The Internet Archive sent a reply that could not be read.",
            "the archive replied with something unreadable",
            {},
        )

    when = _when(chosen)
    original = chosen.get("original") or url
    card = {
        "url": original,
        "read": archived,
        "when": when,
        "chars": len(text or ""),
        "note": note.strip(),
        # The chosen snapshot is the first row, so the rest of the list is what
        # the panel shows under it.
        "older": [_row(r) for r in rows[1:]],
        "reason": reason if not text else "",
    }
    head = f"{note}Archived copy of {original}, saved {when}.\n"
    if not text:
        head += (
            f"The snapshot is listed but its text could not be read ({reason}). "
            f"Snapshots held:\n{_row_text(rows)}"
        )
        return True, head, "", {"card": card}
    sources = [
        {"source": archived, "title": f"archived {when}: {original}", "kind": "web", "read": True}
    ]
    body = (
        f"{head}Snapshot {when} of {url} (Wayback Machine):\n{text}\n\n"
        f"Other snapshots:\n{_row_text(rows[1:]) or '  none'}"
    )
    return True, body, "", {"sources": sources, "card": card}


async def _to_public(url):
    """`is_public_url` resolves the host, which blocks, so it runs off-thread."""
    return await asyncio.to_thread(is_public_url, url)


async def run(args):
    """Handler for the wayback tool. Returns text for the model, never raises."""
    return (await read(args))[1]

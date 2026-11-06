"""Current weather and a short forecast, from open-meteo.

No key and no signup: the geocoder turns a place name into coordinates and the
forecast comes back for those. A small model passes place names loosely, so a
name the geocoder does not know is retried in looser forms before giving up.
"""

import httpx

from . import as_args

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PROVIDER_URL = "https://open-meteo.com/"
TIMEOUT = 10
DEFAULT_DAYS = 3
MAX_DAYS = 7
MAX_CHARS = 4_000
MAX_TRIES = 4

# The WMO scale open-meteo reports in. A number alone tells the model nothing.
WMO = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

# Words a place name picks up that the geocoder does not match on: it knows
# "Bandung", not "Kota Bandung".
QUALIFIERS = {
    "kota",
    "kabupaten",
    "kab",
    "kecamatan",
    "kelurahan",
    "desa",
    "provinsi",
    "prov",
    "daerah",
    "istimewa",
    "city",
    "town",
    "village",
    "district",
    "municipality",
    "county",
    "of",
    "the",
}

TOOL = {
    "type": "function",
    "function": {
        "name": "weather",
        "description": (
            "Current weather and a short forecast for a place, by name. Use it "
            "for any question about temperature, rain, wind, or conditions now "
            "or in the next few days, instead of searching for them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "The place, e.g. 'Bandung' or 'Jakarta, Indonesia'.",
                },
                "days": {
                    "type": "integer",
                    "description": f"Forecast days, 1 to {MAX_DAYS}. Default {DEFAULT_DAYS}.",
                },
            },
            "required": ["location"],
        },
    },
}


def _candidates(name):
    """The name as given, then the looser forms of it worth trying."""
    out = []

    def add(value):
        value = (value or "").strip(" ,")
        if value and value.lower() not in {o.lower() for o in out}:
            out.append(value)

    add(name)
    head = name.split(",")[0]
    add(head)
    for base in (name, head or name):
        words = base.split()
        if len(words) < 2:
            continue
        add(" ".join(w for w in words if w.lower().strip(".") not in QUALIFIERS))
        add(words[-1])
    return out[:MAX_TRIES]


async def _lookup(client, name):
    r = await client.get(
        GEOCODE_URL, params={"name": name, "count": 1, "format": "json"}
    )
    r.raise_for_status()
    results = r.json().get("results") or []
    return results[0] if results else None


async def _find_place(client, name):
    """The first of the name's forms the geocoder knows, or None."""
    for candidate in _candidates(name):
        place = await _lookup(client, candidate)
        if place:
            return place
    return None


def _label(place):
    parts = []
    for part in (place.get("name"), place.get("admin1"), place.get("country")):
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts)


def _num(value):
    if value is None:
        return "?"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value)


def _fig(value):
    """The number itself, or None. The card formats its own figures, so it gets
    values rather than the strings the model reads."""
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _code(value):
    if value is None:
        return "unknown"
    return WMO.get(int(value), f"weather code {int(value)}")


def _at(series, key, i):
    values = series.get(key) or []
    return values[i] if i < len(values) else None


def _render(label, place, data, days):
    lines = [
        f"Weather for {label} ({place.get('latitude')}, {place.get('longitude')}), "
        f"local time {data.get('timezone') or 'unknown'}."
    ]
    now = data.get("current") or {}
    if now:
        lines.append(
            f"Now ({now.get('time', '?')}): {_num(now.get('temperature_2m'))}°C, "
            f"feels like {_num(now.get('apparent_temperature'))}°C, "
            f"{_code(now.get('weather_code'))}, "
            f"humidity {_num(now.get('relative_humidity_2m'))}%, "
            f"wind {_num(now.get('wind_speed_10m'))} km/h, "
            f"rain {_num(now.get('precipitation'))} mm."
        )
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    if dates:
        lines.append(f"Forecast ({len(dates[:days])} days):")
        for i, day in enumerate(dates[:days]):
            lines.append(
                f"  {day}: {_code(_at(daily, 'weather_code', i))}, "
                f"{_num(_at(daily, 'temperature_2m_min', i))} to "
                f"{_num(_at(daily, 'temperature_2m_max', i))}°C, "
                f"rain {_num(_at(daily, 'precipitation_sum', i))} mm "
                f"({_num(_at(daily, 'precipitation_probability_max', i))}% chance)"
            )
    return "\n".join(lines)[:MAX_CHARS]


def _card(label, place, data, days):
    """The card's own payload: figures, not sentences. The model reads `_render`
    and never this; the reader sees this and never the rendered text."""
    now = data.get("current") or {}
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    forecast = []
    for i, day in enumerate(dates[:days]):
        forecast.append(
            {
                "date": str(day),
                "code": _at(daily, "weather_code", i),
                "text": _code(_at(daily, "weather_code", i)),
                "min": _fig(_at(daily, "temperature_2m_min", i)),
                "max": _fig(_at(daily, "temperature_2m_max", i)),
                "rain": _fig(_at(daily, "precipitation_sum", i)),
                "chance": _fig(_at(daily, "precipitation_probability_max", i)),
            }
        )
    return {
        "place": label,
        "lat": _fig(place.get("latitude")),
        "lon": _fig(place.get("longitude")),
        "timezone": data.get("timezone") or "",
        "now": {
            "time": str(now.get("time") or ""),
            "temp": _fig(now.get("temperature_2m")),
            "feels": _fig(now.get("apparent_temperature")),
            "code": now.get("weather_code"),
            "text": _code(now.get("weather_code")),
            "humidity": _fig(now.get("relative_humidity_2m")),
            "wind": _fig(now.get("wind_speed_10m")),
            "rain": _fig(now.get("precipitation")),
        },
        "forecast": forecast,
    }


def _days(args):
    try:
        days = int(args.get("days") or DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return min(max(days, 1), MAX_DAYS)


async def read(args):
    """`(ok, text, reason, meta)`. `reason` is empty on success, and comes back
    apart from the text so the caller can name the failure alone. `meta` is the
    tool result's metadata: the source list, and the figures the card draws."""
    args = as_args(args)
    location = ""
    for key in ("location", "place", "city"):
        location = str(args.get(key) or "").strip()
        if location:
            break
    if not location:
        return False, "No place given.", "no place was given", {}
    days = _days(args)
    params = {
        "latitude": 0,
        "longitude": 0,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
        "precipitation,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_sum,precipitation_probability_max",
        "forecast_days": days,
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            place = await _find_place(client, location)
            if place is None:
                return (
                    False,
                    f"No place named {location!r} was found. Try the plain name of "
                    "the city, or add the country.",
                    "the place name was not found",
                    {},
                )
            params["latitude"] = place.get("latitude")
            params["longitude"] = place.get("longitude")
            r = await client.get(FORECAST_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return (
            False,
            f"The weather service could not be reached ({type(e).__name__}).",
            "the weather service could not be reached",
            {},
        )
    except ValueError:
        return (
            False,
            "The weather service sent a reply that could not be read.",
            "the weather service replied with something unreadable",
            {},
        )
    label = _label(place) or location
    meta = {
        "sources": [
            {"source": PROVIDER_URL, "title": f"{label} — open-meteo", "kind": "web", "read": True}
        ],
        "card": _card(label, place, data, days),
    }
    return True, _render(label, place, data, days), "", meta


async def run(args):
    """Handler for the weather tool. Returns text for the model, never raises."""
    return (await read(args))[1]

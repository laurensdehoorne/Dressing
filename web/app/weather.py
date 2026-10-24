"""Today's and tomorrow's weather for the stylist, from Open-Meteo (free, no key). Plain HTTP, no AI cost."""

import asyncio
import json
import time
import urllib.parse
import urllib.request

TIMEOUT = 8
CACHE_SECONDS = 30 * 60
_cache: dict[tuple, tuple[float, dict]] = {}

# WMO weather codes, as Open-Meteo gives them, in a few Dutch words.
CODES = {
    0: "helder", 1: "overwegend helder", 2: "half bewolkt", 3: "bewolkt", 45: "mist", 48: "mist met rijp",
    51: "lichte motregen", 53: "motregen", 55: "dichte motregen", 56: "ijzel", 57: "ijzel",
    61: "lichte regen", 63: "regen", 65: "zware regen", 66: "ijsregen", 67: "ijsregen",
    71: "lichte sneeuw", 73: "sneeuw", 75: "zware sneeuw", 77: "korrelsneeuw",
    80: "regenbuien", 81: "regenbuien", 82: "zware regenbuien", 85: "sneeuwbuien", 86: "sneeuwbuien",
    95: "onweer", 96: "onweer met hagel", 99: "onweer met hagel",
}


class WeatherError(Exception):
    """A Dutch message that can be shown as-is."""


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Dressing/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read(1_000_000))


def _geocode(place: str) -> dict:
    query = urllib.parse.urlencode({"name": place, "count": 1, "language": "nl", "format": "json"})
    try:
        found = (_get(f"https://geocoding-api.open-meteo.com/v1/search?{query}").get("results") or [])
    except (OSError, ValueError):
        raise WeatherError("De weerdienst is niet bereikbaar.")
    if not found:
        raise WeatherError(f"Plaats ‘{place}’ niet gevonden.")
    r = found[0]
    return {"name": ", ".join(x for x in (r.get("name"), r.get("country")) if x), "lat": r["latitude"], "lon": r["longitude"]}


async def find_place(place: str) -> dict:
    """The place someone typed (a town), with its coordinates."""
    return await asyncio.to_thread(_geocode, place.strip())


def _forecast(lat: float, lon: float) -> dict:
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_min,apparent_temperature_max,"
                 "precipitation_probability_max,wind_speed_10m_max,uv_index_max",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
    })
    try:
        data = _get(f"https://api.open-meteo.com/v1/forecast?{query}")
    except (OSError, ValueError):
        raise WeatherError("De weerdienst is niet bereikbaar.")
    d, now = data.get("daily") or {}, data.get("current") or {}
    days = []
    for i, label in enumerate(("vandaag", "morgen")):
        try:
            days.append({
                "label": label, "date": d["time"][i], "summary": CODES.get(d["weather_code"][i], "wisselend"),
                "min": round(d["temperature_2m_min"][i]), "max": round(d["temperature_2m_max"][i]),
                "feels_min": round(d["apparent_temperature_min"][i]), "feels_max": round(d["apparent_temperature_max"][i]),
                "rain": d["precipitation_probability_max"][i], "wind": round(d["wind_speed_10m_max"][i]),
                "uv": d["uv_index_max"][i],
            })
        except (KeyError, IndexError, TypeError):
            break
    current = None
    if "temperature_2m" in now:
        current = {"temp": round(now["temperature_2m"]), "feels": round(now.get("apparent_temperature", now["temperature_2m"])),
                   "summary": CODES.get(now.get("weather_code"), ""), "wind": round(now.get("wind_speed_10m") or 0)}
    return {"current": current, "days": days}


async def forecast(place: dict) -> dict:
    """Now, today and tomorrow at this place; kept half an hour."""
    key = (round(place["lat"], 2), round(place["lon"], 2))
    if (hit := _cache.get(key)) and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    result = {**await asyncio.to_thread(_forecast, place["lat"], place["lon"]), "place": place["name"]}
    _cache[key] = (time.monotonic(), result)
    return result


def describe(w: dict) -> str:
    """The forecast as lines for the stylist."""
    lines = [f"Weer in {w.get('place', '')}:"]
    if c := w.get("current"):
        lines.append(f"- nu: {c['temp']} °C (voelt als {c['feels']} °C), {c['summary']}, wind {c['wind']} km/u")
    for d in w.get("days", []):
        lines.append(f"- {d['label']} ({d['date']}): {d['summary']}, {d['min']} tot {d['max']} °C (gevoel {d['feels_min']} tot "
                     f"{d['feels_max']} °C), kans op neerslag {d['rain']} %, wind tot {d['wind']} km/u, uv-index {d['uv']}")
    return "\n".join(lines)

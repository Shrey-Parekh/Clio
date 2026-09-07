"""Current conditions, from Open-Meteo - no API key, no account.

Nothing is requested until a location is configured. Coordinates are the one
piece of genuinely personal data these utilities would send anywhere, so it is
opt-in by editing the config rather than guessed from the IP address, and
saying "I don't know where you are" is the honest answer until then.
"""

from __future__ import annotations

import re

from clio.capabilities.calculate import format_number
from clio.core.config import LocationConfig
from clio.core.http import get_json

_API = "https://api.open-meteo.com/v1/forecast"

_WEATHER_PHRASES = re.compile(
    r"\b(?:what'?s? the weather|how'?s the weather|the weather|weather outside|"
    r"is it raining|is it going to rain|how (?:hot|cold|warm) is it|"
    r"what'?s? it like outside|temperature outside)\b"
)

# WMO codes, as returned in `weather_code`.
_CONDITIONS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog",
    51: "drizzling lightly", 53: "drizzling", 55: "drizzling heavily",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "raining lightly", 63: "raining", 65: "raining heavily",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "snowing lightly", 73: "snowing", 75: "snowing heavily",
    77: "hailing",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "severe thunderstorms with hail",
}


def is_weather_query(text: str) -> bool:
    return _WEATHER_PHRASES.search(" ".join(text.lower().split())) is not None


async def describe_weather(location: LocationConfig) -> str:
    if not location.configured:
        return (
            "I don't know where you are yet. Set your latitude and longitude "
            "under location in config slash default dot toml, and I'll be able to check."
        )

    payload = await get_json(
        _API,
        {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        },
    )

    current = payload.get("current") or {}
    temperature = current.get("temperature_2m")
    if temperature is None:
        raise ValueError("weather response carried no current temperature")

    condition = _CONDITIONS.get(current.get("weather_code"), "hard to categorise")
    where = f" in {location.name}" if location.name else ""
    reply = f"It's {format_number(float(temperature), places=0)} degrees{where}, and {condition}."

    feels = current.get("apparent_temperature")
    if feels is not None and abs(float(feels) - float(temperature)) >= 3:
        reply += f" Feels more like {format_number(float(feels), places=0)}."
    return reply

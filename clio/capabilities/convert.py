"""Unit conversion, offline and exact — a factor is a constant, so a model would
only trade a correct answer for a plausible one.

Each dimension converts through a base unit. Temperature is affine, not a ratio,
so it converts through celsius instead of a factor.
"""

from __future__ import annotations

import re

from clio.capabilities.calculate import format_number

# alias -> (dimension, factor to the dimension's base unit, spoken name)
_UNITS: dict[str, tuple[str, float, str]] = {}


def _add(dimension: str, factor: float, spoken: str, *aliases: str) -> None:
    for alias in aliases:
        _UNITS[alias] = (dimension, factor, spoken)


# length, base metre
_add("length", 1.0, "metres", "metre", "metres", "meter", "meters", "m")
_add("length", 1000.0, "kilometres", "kilometre", "kilometres", "kilometer", "kilometers", "km", "kms")
_add("length", 0.01, "centimetres", "centimetre", "centimetres", "centimeter", "centimeters", "cm")
_add("length", 0.001, "millimetres", "millimetre", "millimetres", "millimeter", "millimeters", "mm")
_add("length", 1609.344, "miles", "mile", "miles")
_add("length", 0.9144, "yards", "yard", "yards")
_add("length", 0.3048, "feet", "foot", "feet", "ft")
_add("length", 0.0254, "inches", "inch", "inches")
_add("length", 1852.0, "nautical miles", "nautical mile", "nautical miles")

# mass, base kilogram
_add("mass", 1.0, "kilograms", "kilogram", "kilograms", "kilo", "kilos", "kg", "kgs")
_add("mass", 0.001, "grams", "gram", "grams", "g")
_add("mass", 1e-6, "milligrams", "milligram", "milligrams", "mg")
_add("mass", 0.45359237, "pounds", "pound", "pounds", "lb", "lbs")
_add("mass", 0.028349523125, "ounces", "ounce", "ounces", "oz")
_add("mass", 6.35029318, "stone", "stone", "stones")
_add("mass", 1000.0, "tonnes", "tonne", "tonnes", "metric ton", "metric tons")

# volume, base litre
_add("volume", 1.0, "litres", "litre", "litres", "liter", "liters")
_add("volume", 0.001, "millilitres", "millilitre", "millilitres", "milliliter", "milliliters", "ml")
_add("volume", 3.785411784, "gallons", "gallon", "gallons")
_add("volume", 0.473176473, "pints", "pint", "pints")
_add("volume", 0.2365882365, "cups", "cup", "cups")

# speed, base metres per second
_add("speed", 1.0, "metres per second", "metres per second", "meters per second")
_add("speed", 0.277777778, "kilometres per hour", "kilometres per hour", "kilometers per hour", "kph")
_add("speed", 0.44704, "miles per hour", "miles per hour", "mph")
_add("speed", 0.514444444, "knots", "knot", "knots")

# data, base byte. Powers of 1024: the sizes a file manager reports.
_add("data", 1.0, "bytes", "byte", "bytes")
_add("data", 1024.0, "kilobytes", "kilobyte", "kilobytes", "kb")
_add("data", 1024.0**2, "megabytes", "megabyte", "megabytes", "mb")
_add("data", 1024.0**3, "gigabytes", "gigabyte", "gigabytes", "gb")
_add("data", 1024.0**4, "terabytes", "terabyte", "terabytes", "tb")

# Affine, so these carry no factor and convert through celsius.
_TEMPERATURES = {
    "celsius": "celsius", "centigrade": "celsius",
    "fahrenheit": "fahrenheit", "kelvin": "kelvin",
}
_TEMPERATURE_SPOKEN = {
    "celsius": "degrees celsius",
    "fahrenheit": "degrees fahrenheit",
    "kelvin": "kelvin",
}

_NUMBER = r"(?P<value>-?\d+(?:\.\d+)?)"


def _alternation() -> str:
    # Longest first, so "nautical miles" is not matched as "miles" and
    # "kilometres per hour" not as "kilometres".
    names = sorted(list(_UNITS) + list(_TEMPERATURES), key=len, reverse=True)
    return "|".join(re.escape(n) for n in names)


_UNIT = _alternation()
_FORWARD = re.compile(
    rf"{_NUMBER}\s*(?:degrees\s+)?(?P<source>{_UNIT})\b\s+"
    rf"(?:to|in|into|as)\s+(?:degrees\s+)?(?P<target>{_UNIT})\b"
)
_REVERSE = re.compile(
    rf"how many\s+(?:degrees\s+)?(?P<target>{_UNIT})\b\s+(?:are|is|in)\s+(?:there\s+)?(?:in\s+)?"
    rf"{_NUMBER}\s*(?:degrees\s+)?(?P<source>{_UNIT})\b"
)


def parse_conversion(text: str) -> tuple[float, str, str] | None:
    """(value, source, target) if `text` asks to convert between two units of
    the same kind, else None. Mismatched dimensions are refused here."""
    lowered = " ".join(text.lower().split())
    match = _FORWARD.search(lowered) or _REVERSE.search(lowered)
    if match is None:
        return None

    source, target = match.group("source"), match.group("target")
    if _dimension(source) != _dimension(target) or source == target:
        return None
    return float(match.group("value")), source, target


def convert(value: float, source: str, target: str) -> float:
    if _dimension(source) == "temperature":
        return _from_celsius(_to_celsius(value, _TEMPERATURES[source]), _TEMPERATURES[target])
    return value * _UNITS[source][1] / _UNITS[target][1]


def format_conversion(value: float, source: str, target: str) -> str:
    return f"{format_number(convert(value, source, target), places=2)} {_spoken(target)}."


def _dimension(alias: str) -> str:
    return "temperature" if alias in _TEMPERATURES else _UNITS[alias][0]


def _spoken(alias: str) -> str:
    if alias in _TEMPERATURES:
        return _TEMPERATURE_SPOKEN[_TEMPERATURES[alias]]
    return _UNITS[alias][2]


def _to_celsius(value: float, scale: str) -> float:
    if scale == "fahrenheit":
        return (value - 32.0) * 5.0 / 9.0
    if scale == "kelvin":
        return value - 273.15
    return value


def _from_celsius(celsius: float, scale: str) -> float:
    if scale == "fahrenheit":
        return celsius * 9.0 / 5.0 + 32.0
    if scale == "kelvin":
        return celsius + 273.15
    return celsius

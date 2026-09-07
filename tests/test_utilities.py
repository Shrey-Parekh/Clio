"""Deterministic utilities: arithmetic, units, currency, weather. Nothing here
may reach the LLM, and nothing here touches the real network.
Run: python tests/test_utilities.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities import currency as currency_mod  # noqa: E402
from clio.capabilities import weather as weather_mod  # noqa: E402
from clio.capabilities.calculate import format_number, parse_calculation  # noqa: E402
from clio.capabilities.convert import convert, parse_conversion  # noqa: E402
from clio.capabilities.currency import parse_currency_request  # noqa: E402
from clio.capabilities.weather import describe_weather, is_weather_query  # noqa: E402
from clio.core.config import LocationConfig  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tier="default"):
        self.calls += 1
        return "llm reply"


def build():
    llm = FakeLLM()
    o = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0,
    )
    o._memory = ConversationMemory(provider=llm, system_prompt="p")
    return o, llm


async def main():
    # --- arithmetic ---

    for text, want in [
        ("what is 15 percent of 240", 36.0),
        ("12 times 7", 84.0),
        ("what's 2 plus 2", 4.0),
        ("2 to the power of 10", 1024.0),
        ("what is the capital of france", None),
        ("set a timer for five minutes", None),
        ("1 divided by 0", None),
        ("__import__('os').system('echo hi')", None),
        ("2 ** 999999999", None),
    ]:
        assert parse_calculation(text) == want, (text, parse_calculation(text))
    print("OK  arithmetic parsed, prose and unsafe input refused rather than guessed")

    assert format_number(4.0) == "4" and format_number(14.285714) == "14.2857"
    print("OK  results read cleanly aloud")

    # --- units ---

    assert abs(convert(5, "miles", "km") - 8.04672) < 1e-4
    assert abs(convert(100, "fahrenheit", "celsius") - 37.7778) < 1e-3, "temperature is affine"
    assert abs(convert(0, "celsius", "kelvin") - 273.15) < 1e-9
    assert parse_conversion("5 miles in kilograms") is None, "dimensions must match"
    # Asked in reverse, but still 5 miles converted into kilometres.
    assert parse_conversion("how many kilometers in 5 miles") == (5.0, "miles", "kilometers")
    print("OK  units convert exactly, mismatched dimensions refused")

    # --- currency, without touching the network ---

    assert parse_currency_request("convert 100 dollars to rupees") == (100.0, "USD", "INR")
    assert parse_currency_request("50 euros in pounds") == (50.0, "EUR", "GBP")
    assert parse_currency_request("100 dollars to usd") is None, "same currency is not a conversion"

    async def fake_rates(url, params, timeout_s=6.0):
        assert "frankfurter" in url and params["from"] == "USD"
        return {"amount": 100.0, "base": "USD", "date": "2026-09-05", "rates": {"INR": 8300.0}}

    currency_mod.get_json = fake_rates
    o, llm = build()
    spoken, used = await o._handle_utterance("convert 100 dollars to rupees")
    assert used is False and llm.calls == 0, "a conversion must never cost an LLM call"
    assert "8300 INR" in spoken and "2026-09-05" in spoken, spoken
    print(f"OK  currency answered from live rates, zero LLM calls: {spoken!r}")

    # --- weather ---

    assert is_weather_query("what's the weather") and not is_weather_query("what's the plan")

    unset = LocationConfig(name="", latitude=0.0, longitude=0.0)
    assert not unset.configured
    said = await describe_weather(unset)
    assert "don't know where you are" in said, said
    print("OK  no location configured means saying so, and sending nothing")

    async def fake_forecast(url, params, timeout_s=6.0):
        assert "open-meteo" in url and params["latitude"] == 19.07
        return {"current": {"temperature_2m": 21.4, "apparent_temperature": 26.0, "weather_code": 3}}

    weather_mod.get_json = fake_forecast
    said = await describe_weather(LocationConfig(name="Mumbai", latitude=19.07, longitude=72.87))
    assert "21 degrees in Mumbai" in said and "overcast" in said, said
    assert "Feels more like 26" in said, "a big apparent-temperature gap is worth saying"
    print(f"OK  weather read from coordinates: {said!r}")

    # --- the registry knows which of these survive an outage ---

    caps = {c.name: c.offline for c in o._router.capabilities()}
    assert caps["calculate"] and caps["convert"], "arithmetic and units are local"
    assert not caps["currency"] and not caps["weather"], "these need the network"
    print("OK  offline claims match reality")

    print("\nAll utility checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

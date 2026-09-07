"""Currency conversion against live rates.

The one utility here that cannot be offline: a rate is a fact about today, not
a constant, and a cached one quoted confidently is worse than saying the rate
is unavailable. Frankfurter publishes the ECB's daily reference rates and needs
no API key or account, so nothing has to be registered or kept secret; the
request carries an amount and two currency codes and nothing else.

Rates are the ECB's daily fix, not a live trading price - fine for "how much is
that in rupees", not for anything that settles money.
"""

from __future__ import annotations

import re

from clio.capabilities.calculate import format_number
from clio.core.http import get_json

_API = "https://api.frankfurter.app/latest"

# Spoken name -> ISO code. Only the ones worth saying out loud; the rest of the
# ECB list is reachable by code.
_CURRENCIES = {
    "dollar": "USD", "dollars": "USD", "usd": "USD", "us dollars": "USD",
    "euro": "EUR", "euros": "EUR", "eur": "EUR",
    "pound": "GBP", "pounds": "GBP", "gbp": "GBP", "sterling": "GBP", "quid": "GBP",
    "rupee": "INR", "rupees": "INR", "inr": "INR",
    "yen": "JPY", "jpy": "JPY",
    "yuan": "CNY", "renminbi": "CNY", "cny": "CNY",
    "franc": "CHF", "francs": "CHF", "chf": "CHF",
    "canadian dollars": "CAD", "cad": "CAD",
    "australian dollars": "AUD", "aud": "AUD",
    "krona": "SEK", "sek": "SEK",
    "zloty": "PLN", "pln": "PLN",
    "real": "BRL", "reais": "BRL", "brl": "BRL",
    "rand": "ZAR", "zar": "ZAR",
    "won": "KRW", "krw": "KRW",
    "peso": "MXN", "pesos": "MXN", "mxn": "MXN",
}

_NAMES = "|".join(re.escape(n) for n in sorted(_CURRENCIES, key=len, reverse=True))
_REQUEST = re.compile(
    rf"(?P<value>\d+(?:\.\d+)?)\s*(?P<source>{_NAMES})\b\s+(?:to|in|into)\s+(?P<target>{_NAMES})\b"
)


def parse_currency_request(text: str) -> tuple[float, str, str] | None:
    """Returns (amount, source code, target code), or None when this isn't
    clearly a currency conversion."""
    lowered = " ".join(text.lower().split())
    match = _REQUEST.search(lowered)
    if match is None:
        return None

    source, target = _CURRENCIES[match.group("source")], _CURRENCIES[match.group("target")]
    if source == target:
        return None
    return float(match.group("value")), source, target


async def convert_currency(amount: float, source: str, target: str) -> str:
    payload = await get_json(_API, {"amount": amount, "from": source, "to": target})

    rates = payload.get("rates") or {}
    converted = rates.get(target)
    if converted is None:
        # A 200 with the pair missing means the API knows the codes but not
        # this pair - saying so beats reading `None` aloud.
        raise ValueError(f"no published rate for {source} to {target}")

    on = payload.get("date", "")
    return (
        f"{format_number(amount, places=2)} {source} is about "
        f"{format_number(float(converted), places=2)} {target}, "
        f"at the European Central Bank's rate for {on}."
    )

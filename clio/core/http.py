"""One JSON GET, with a timeout, off the event loop.

`urllib` rather than a client library: two capabilities need one request each,
and the stdlib already does it. It blocks, so it runs on a thread - the loop it
would otherwise stall is the one carrying the microphone.

Failures are left as `urllib.error.URLError`, which `describe_error` already
classifies as a network problem and speaks as one.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request

_TIMEOUT_S = 6.0
_MAX_BYTES = 1_000_000

# Some public APIs reject the default Python agent outright.
_HEADERS = {"User-Agent": "Clio/0.1 (personal voice assistant)", "Accept": "application/json"}


async def get_json(url: str, params: dict[str, object], timeout_s: float = _TIMEOUT_S) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    return await asyncio.to_thread(_fetch, full, timeout_s)


def _fetch(url: str, timeout_s: float) -> dict:
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload = json.loads(response.read(_MAX_BYTES).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object from {url}, got {type(payload).__name__}")
    return payload

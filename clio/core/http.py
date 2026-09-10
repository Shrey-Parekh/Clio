"""JSON over HTTP - a GET or a POST - with a timeout, off the event loop.

`urllib` (stdlib) rather than a client library; it blocks, so it runs on a
thread to keep the mic loop free. Failures stay `urllib.error.URLError` (an
HTTP status is its subclass `HTTPError`), which `describe_error` already speaks
as a network problem.
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
    return await asyncio.to_thread(_send, urllib.request.Request(full, headers=_HEADERS), timeout_s)


async def post_json(
    url: str, body: dict, headers: dict[str, str] | None = None, timeout_s: float = _TIMEOUT_S
) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={**_HEADERS, "Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    return await asyncio.to_thread(_send, request, timeout_s)


def _send(request: urllib.request.Request, timeout_s: float) -> dict:
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload = json.loads(response.read(_MAX_BYTES).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object from {request.full_url}, got {type(payload).__name__}")
    return payload

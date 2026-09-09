"""Am I online, what am I connected to, and what is my address.

Distinct from `status` (is the model reachable) and `system` (hardware). It
must work when the answer is no, so reachability is a short-timeout socket
connect, never a request that can hang. Local IP is the default; the public one
(which leaves the house) is fetched only when asked for by name.
"""

from __future__ import annotations

import asyncio
import re
import socket
import subprocess

from clio.core.http import get_json

_STRIP = re.compile(r"[.!?,;:]+$")

# Cloudflare's resolver on the DNS port: a TCP connect that either completes or
# refuses quickly. No payload, and no DNS lookup of its own to get stuck on.
_PROBE = ("1.1.1.1", 53)
_PROBE_TIMEOUT_S = 1.5
_PUBLIC_IP_API = "https://api.ipify.org"

_PATTERNS: list[tuple[str, str]] = [
    ("public_ip", r"(?:what(?:'?s| is) my )?(?:public|external|real) ip"),
    ("wifi", r"(?:what|which) (?:wi-?fi|network|ssid).*(?:on|connected)|"
             r"what(?:'?s| is) (?:the |my )?(?:wi-?fi|ssid|network) (?:called|name)|"
             r"what wi-?fi am i on"),
    ("local_ip", r"what(?:'?s| is) my ip(?: address)?|my ip address"),
    ("online", r"am i (?:online|connected|on the internet)|"
               r"is the (?:internet|wi-?fi|network) (?:up|working|down|out)|"
               r"do i have internet"),
]

_COMPILED = [(kind, re.compile(p)) for kind, p in _PATTERNS]


def parse_network_request(text: str) -> str | None:
    lowered = " ".join(_STRIP.sub("", text.strip().lower()).split())
    for kind, pattern in _COMPILED:
        if pattern.search(lowered):
            return kind
    return None


def is_online(timeout_s: float = _PROBE_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection(_PROBE, timeout=timeout_s):
            return True
    except OSError:
        return False


def local_ip() -> str:
    """This machine's LAN address. UDP connect() only picks a route and sends
    nothing, so it works with the network down and costs no packets."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(_PROBE)
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def wifi_name() -> str:
    """Empty means wired, wifi off, or no wireless adapter - all of which are
    answered as "not on wifi" rather than as a failure."""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=5.0,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return ""
    if "connected" not in result.stdout.lower():
        return ""
    for line in result.stdout.splitlines():
        # "BSSID" also contains SSID, so the prefix is what distinguishes them.
        if line.strip().startswith("SSID"):
            return line.split(":", 1)[1].strip()
    return ""


def _spoken_ip(address: str) -> str:
    """Read as "192 dot 168 dot 1 dot 40". Without the spacing a TTS engine
    runs the octets together into one long number."""
    return " dot ".join(address.split("."))


async def describe_network(kind: str) -> str:
    if kind == "online":
        if not await asyncio.to_thread(is_online):
            return "No internet. Everything local still works."
        wifi = await asyncio.to_thread(wifi_name)
        return f"Online, on {wifi}." if wifi else "Online, on a wired connection."

    if kind == "wifi":
        wifi = await asyncio.to_thread(wifi_name)
        return f"You're on {wifi}." if wifi else "Not on wifi - wired, or the adapter's off."

    if kind == "local_ip":
        address = await asyncio.to_thread(local_ip)
        if not address:
            return "I can't work out this machine's address - the network looks down."
        return f"This machine is {_spoken_ip(address)} on the local network."

    # The only lookup that leaves the house, and can fail on the very thing asked.
    if not await asyncio.to_thread(is_online):
        return "I can't check that with the internet down."
    payload = await get_json(_PUBLIC_IP_API, {"format": "json"})
    address = payload.get("ip", "")
    if not address:
        return "The lookup came back empty."
    return f"Your public address is {_spoken_ip(address)}."

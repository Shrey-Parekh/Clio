"""The core WebSocket server: a real round trip from an event on the bus to
JSON at a connected client, bound to localhost only.

Run: python tests/test_server.py
"""

import asyncio
import json
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.core.events import EventBus  # noqa: E402
from clio.core.server import CoreServer  # noqa: E402


async def main():
    bus = EventBus()
    server = CoreServer(bus, port=0)  # 0 -> OS picks a free port
    await server.start()
    try:
        assert server.port > 0, "a real port was bound"

        # --- an event on the bus arrives as JSON at a connected client ---

        async with websockets.connect(f"ws://127.0.0.1:{server.port}") as client:
            await asyncio.sleep(0.05)  # let the server register the connection
            await bus.publish("clio.wake", {"phrase": "hey clio"}, source="test")
            raw = await asyncio.wait_for(client.recv(), timeout=2.0)

        message = json.loads(raw)
        assert message["name"] == "clio.wake", message
        assert message["payload"] == {"phrase": "hey clio"}, message
        assert message["source"] == "test" and isinstance(message["ts"], (int, float))
        print(f"OK  an event reached the client as JSON: {message['name']}")

        # --- a disconnected client is dropped, and broadcasting still works ---

        await asyncio.sleep(0.05)
        assert len(server._clients) == 0, "the closed client was pruned"
        # No clients: a publish must not raise.
        await bus.publish("clio.idle", {}, source="test")
        print("OK  a closed client is pruned and an empty broadcast is a no-op")

        # --- a non-JSON-native payload value degrades rather than breaking ---

        async with websockets.connect(f"ws://127.0.0.1:{server.port}") as client:
            await asyncio.sleep(0.05)
            await bus.publish("clio.odd", {"when": object()}, source="test")
            message = json.loads(await asyncio.wait_for(client.recv(), timeout=2.0))
            assert "object at 0x" in message["payload"]["when"], message
        print("OK  an unserialisable payload value is stringified, not dropped")

        print("\nAll server checks passed.")
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())

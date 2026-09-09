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
    finally:
        await server.stop()

    # --- a command from the frontend reaches the handler and gets a reply ---

    seen = []

    async def handler(command):
        seen.append(command)
        return {"facts": ["likes tea"]} if command.get("cmd") == "get_memory" else None

    commanded = CoreServer(bus, port=0, command_handler=handler)
    await commanded.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{commanded.port}") as client:
            await asyncio.sleep(0.05)
            await client.send(json.dumps({"cmd": "get_memory", "id": 7}))
            reply = json.loads(await asyncio.wait_for(client.recv(), timeout=2.0))
        assert seen and seen[0]["cmd"] == "get_memory", seen
        assert reply["name"] == "clio.reply" and reply["req"] == 7, reply
        assert reply["payload"]["facts"] == ["likes tea"], reply
        print("OK  a command reaches the handler and its reply returns to the sender")
    finally:
        await commanded.stop()

    print("\nAll server checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

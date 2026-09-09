"""A localhost WebSocket server that broadcasts the event bus to the frontend.

The Tauri shell (Phase 5) and anything else local subscribes here to see what
Clio is doing — waking, thinking, speaking — without being wired into the core.
One-way for now: the frontend observes; commands from it come in a later phase.

Bound to 127.0.0.1 only. The bus carries transcripts and state, so it must not
be reachable from the network; a local frontend is the only intended reader.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import websockets

from clio.core.events import Event, EventBus
from clio.core.logging import get_logger

log = get_logger("clio.server")

# A command from the frontend -> an optional reply payload sent back to it.
CommandHandler = Callable[[dict], Awaitable[dict | None]]


class CoreServer:
    def __init__(
        self, bus: EventBus, port: int, host: str = "127.0.0.1",
        command_handler: CommandHandler | None = None,
    ) -> None:
        self._bus = bus
        self._host = host
        self._port = port
        self._command_handler = command_handler
        self._clients: set = set()
        self._server = None
        self._unsubscribe = None

    @property
    def port(self) -> int:
        """The bound port — the configured one, or the OS-assigned one if 0 was asked for."""
        if self._server is not None and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self._port

    async def start(self) -> None:
        self._server = await websockets.serve(self._handle, self._host, self._port)
        self._unsubscribe = self._bus.subscribe("*", self._broadcast)
        log.info("Core server listening", extra={"extra_fields": {"host": self._host, "port": self.port}})

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, connection) -> None:
        self._clients.add(connection)
        log.info("Frontend connected", extra={"extra_fields": {"clients": len(self._clients)}})
        try:
            async for raw in connection:  # stays open until the client closes
                await self._dispatch(connection, raw)
        finally:
            self._clients.discard(connection)
            log.info("Frontend disconnected", extra={"extra_fields": {"clients": len(self._clients)}})

    async def _dispatch(self, connection, raw) -> None:
        if self._command_handler is None:
            return
        try:
            command = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        try:
            reply = await self._command_handler(command)
        except Exception:
            log.exception("Command failed", extra={"extra_fields": {"cmd": command.get("cmd")}})
            return
        if reply is not None:
            await connection.send(json.dumps(
                {"name": "clio.reply", "req": command.get("id"), "payload": reply}, default=str))

    async def _broadcast(self, event: Event) -> None:
        if not self._clients:
            return
        # default=str so an odd payload value degrades to its text rather than
        # breaking the broadcast for every client.
        message = json.dumps(
            {"name": event.name, "payload": event.payload, "source": event.source, "ts": event.timestamp},
            default=str,
        )
        dead = []
        for connection in self._clients:
            try:
                await connection.send(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self._clients.discard(connection)

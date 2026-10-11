"""WebSocket fan-out. One subscription to the live channel per API process relays every
live message to the browsers connected to that process.

With Redis (REDIS_URL=redis://...) the channel is Redis Pub/Sub, so ingest can land on one
API replica while the browser is connected to another. With REDIS_URL=memory:// it is an
in-process queue - same code path, no extra service, one API process."""
from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

from ..cache import cache

log = logging.getLogger("drivesense.live")


class LiveHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def _send(self, ws: WebSocket, text: str) -> None:
        try:
            await asyncio.wait_for(ws.send_text(text), timeout=2)
        except Exception:
            self.disconnect(ws)

    async def run(self) -> None:
        while True:
            try:
                async for data in cache.listen():
                    if self.clients:
                        await asyncio.gather(*(self._send(ws, data) for ws in list(self.clients)))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Live listener crashed; restarting in 1 s")
                await asyncio.sleep(1)


hub = LiveHub()

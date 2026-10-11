"""Notifications: stored in PostgreSQL (history) and pushed live through Redis Pub/Sub."""
from __future__ import annotations

from datetime import datetime, timezone

from ..cache import cache
from ..db import db


async def notify(vehicle_id: str, kind: str, severity: str, title: str, body: str = "",
                 meta: dict | None = None, at: datetime | None = None) -> dict:
    row = await db.pool.fetchrow(
        """INSERT INTO notifications (vehicle_id, time, kind, severity, title, body, meta)
           VALUES ($1, $2, $3, $4, $5, $6, $7)
           RETURNING id, vehicle_id, time, kind, severity, title, body, is_read, meta""",
        vehicle_id, at or datetime.now(timezone.utc), kind, severity, title, body, meta or {})
    data = dict(row)
    data["time"] = data["time"].isoformat()
    await cache.publish("notification", vehicle_id, data)
    return data

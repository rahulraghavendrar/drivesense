"""Demo controls for interviews: start trips, inject faults, seed a leaderboard population.

These only queue commands; the fake sensor feeder receives them in its next /api/ingest
response and changes how it simulates the car. The backend never fakes data itself.
"""
import statistics
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..ai.llm import llm
from ..cache import cache
from ..db import db
from ..schemas import DemoFault, DemoTrip
from ..services.ingest import ingest_service

router = APIRouter(prefix="/api", tags=["demo"])


class Population(BaseModel):
    count: int = Field(default=20000, ge=0, le=200000)


def _check(vid: str) -> None:
    if vid not in ingest_service.vehicles:
        raise HTTPException(404, "vehicle not found")


@router.post("/demo/{vid}/trip")
async def demo_trip(vid: str, body: DemoTrip):
    _check(vid)
    await cache.push_command(vid, {"action": "start_trip", "style": body.style, "short": body.short})
    return {"queued": "start_trip", "style": body.style}


@router.post("/demo/{vid}/fault")
async def demo_fault(vid: str, body: DemoFault):
    _check(vid)
    await cache.push_command(vid, {"action": "inject_fault", "fault": body.fault})
    return {"queued": "inject_fault", "fault": body.fault}


@router.post("/demo/{vid}/clear-faults")
async def demo_clear(vid: str):
    _check(vid)
    await cache.push_command(vid, {"action": "clear_faults"})
    return {"queued": "clear_faults"}


@router.post("/demo/population")
async def population(body: Population):
    """Adds N synthetic 'community drivers' to the leaderboards (sorted sets in the hot layer)."""
    own_models = sorted({v["model"] for v in ingest_service.vehicles.values()})
    added = await cache.seed_population(body.count, own_models)
    return {"added": added}


@router.post("/demo/reset")
async def reset():
    """Wipe all data (used by `fake_sensors.py --reset`)."""
    await ingest_service.flush()
    await db.pool.execute("TRUNCATE telemetry, driving_events, trips, notifications, health_alerts, geofences, "
                          "ai_logs, vehicles RESTART IDENTITY CASCADE")
    await cache.reset()
    ingest_service.reset()
    ingest_service.vehicles.clear()
    return {"ok": True}


@router.get("/system/status")
async def status():
    from datetime import datetime, timezone
    kb = await db.pool.fetchrow(
        "SELECT count(*) AS chunks, count(embedding) AS embedded FROM kb_chunks")
    t0 = time.perf_counter()
    await db.pool.fetchval("SELECT 1")
    db_rtt_ms = round((time.perf_counter() - t0) * 1000, 1)
    last = ingest_service.last_ingest_at
    lat = ingest_service.ingest_ms
    return {
        "database": {"provider": db.provider, "version": db.server_version.split()[0],
                     "timescaledb": db.has_timescale, "time_index": "hypertable" if db.has_timescale else "brin",
                     "pooler": db.pooler, "pgvector": True, "round_trip_ms": db_rtt_ms},
        "hot_layer": cache.kind,          # "redis" or "memory"
        "redis": cache.kind == "redis",
        "llm": {"enabled": llm.enabled, "model": llm.model if llm.enabled else None,
                "embed_model": llm.embed_model if llm.enabled else None},
        "knowledge_base": dict(kb),
        "simulator_online": bool(last and (datetime.now(timezone.utc) - last).total_seconds() < 10),
        "last_ingest_at": last.isoformat() if last else None,
        "readings_ingested": ingest_service.readings_total,
        "ingest_latency_ms": {"p50": round(statistics.median(lat), 2) if lat else None,
                              "p95": round(sorted(lat)[int(len(lat) * 0.95) - 1], 2) if len(lat) >= 20 else None},
    }

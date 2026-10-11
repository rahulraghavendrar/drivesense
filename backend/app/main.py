"""DriveSense API - FastAPI application entry point.

Run (from the backend/ folder):  uvicorn app.main:app --reload --port 8000
Interactive API docs:             http://localhost:8000/docs
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .ai.kb import sync_knowledge_base
from .ai.llm import llm
from .cache import cache
from .config import settings
from .db import db
from .routers import ai, demo, ingest, trips, vehicles
from .services.ingest import ingest_service
from .services.live import hub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("drivesense")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect(settings.database_url)
    await cache.connect(settings.redis_url)
    await ingest_service.load()
    log.info("Gemini: %s", f"enabled ({llm.model})" if llm.enabled else "disabled - offline answers")
    tasks = [
        asyncio.create_task(hub.run(), name="live-hub"),
        asyncio.create_task(ingest_service.run_flusher(), name="telemetry-flusher"),
        asyncio.create_task(ingest_service.run_housekeeping(), name="housekeeping"),
        asyncio.create_task(sync_knowledge_base(), name="kb-sync"),
    ]
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await ingest_service.flush()
    await cache.close()
    await db.close()


app = FastAPI(title="DriveSense API", version="1.0.0",
              description="Connected-car telemetry, driving score, predictive health and an AI driving coach.",
              lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])

for r in (ingest.router, vehicles.router, trips.router, ai.router, demo.router):
    app.include_router(r)


@app.get("/api/health", tags=["system"])
async def healthcheck():
    return {"ok": True}


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    """Live stream for the dashboard: telemetry, driving events, notifications, trip completions."""
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()   # keep-alive pings from the client
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(ws)

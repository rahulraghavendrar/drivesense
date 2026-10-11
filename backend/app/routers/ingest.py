"""Device-facing API: the only endpoints a vehicle (or the fake sensor feeder) calls."""
from fastapi import APIRouter, Depends, Header, HTTPException

from ..config import settings
from ..schemas import IngestBatch, RegisterBatch
from ..services.ingest import ingest_service

router = APIRouter(prefix="/api", tags=["device"])


def require_device(x_device_key: str = Header(default="")) -> None:
    if x_device_key != settings.device_key:
        raise HTTPException(status_code=401, detail="invalid device key")


@router.post("/vehicles/register", dependencies=[Depends(require_device)])
async def register(batch: RegisterBatch):
    await ingest_service.register(batch.vehicles)
    return {"registered": [v.id for v in batch.vehicles]}


@router.post("/ingest", dependencies=[Depends(require_device)])
async def ingest(batch: IngestBatch):
    """Accepts a batch of readings. The response carries any queued commands for the
    vehicles in the batch (remote lock, demo faults ...) - a simple 'downlink'."""
    return await ingest_service.ingest(batch.readings, batch.poll)

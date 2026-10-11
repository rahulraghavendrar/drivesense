"""GenAI endpoints: trip coach, health diagnosis, coach chat, knowledge-base tools."""
from fastapi import APIRouter, HTTPException

from ..ai import coach
from ..ai.kb import sync_knowledge_base
from ..ai.retriever import hybrid_search
from ..schemas import CoachChatIn

router = APIRouter(prefix="/api", tags=["ai"])


@router.post("/trips/{tid}/coach")
async def trip_coach(tid: int, refresh: bool = False):
    try:
        return await coach.explain_trip(tid, refresh)
    except coach.NotFound as exc:
        raise HTTPException(404, str(exc))


@router.post("/alerts/{aid}/diagnose")
async def diagnose(aid: int, refresh: bool = False):
    try:
        return await coach.diagnose_alert(aid, refresh)
    except coach.NotFound as exc:
        raise HTTPException(404, str(exc))


@router.post("/vehicles/{vid}/report")
async def report(vid: str, refresh: bool = False):
    """AI weekly report for the car: driving, fuel and health in one analysis."""
    try:
        return await coach.weekly_report(vid, refresh)
    except coach.NotFound as exc:
        raise HTTPException(404, str(exc))


@router.post("/coach/chat")
async def coach_chat(body: CoachChatIn):
    try:
        return await coach.chat(body.vehicle_id, body.question, [h.model_dump() for h in body.history])
    except coach.NotFound as exc:
        raise HTTPException(404, str(exc))


@router.get("/kb/search")
async def kb_search(q: str, collection: str | None = None, k: int = 4):
    """Debug view of the retriever: shows which passages RAG would hand to the model."""
    cols = [collection] if collection else ["coach", "service"]
    return await hybrid_search(q, cols, max(1, min(k, 10)))


@router.post("/kb/sync")
async def kb_sync():
    return await sync_knowledge_base()

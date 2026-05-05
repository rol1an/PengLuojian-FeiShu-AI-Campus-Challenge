import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from pydantic import BaseModel

from app.config import settings
from app.workflow_premeet.scheduler import create_scheduler, _pushed_events, _run_premeet_pipeline
from app.workflow_postmeet.pipeline import handle_meeting_end_event, run_postmeet_pipeline_for_meeting
from app.workflow_qa.qa_agent import handle_user_message, handle_card_action
from app.workflow_qa.context_store import context_store
from app.event_dispatcher import listen_all_events

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Start pre-meeting scheduler
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("Pre-meeting scheduler started (interval=%ds)", settings.PREMEET_POLL_INTERVAL)

    # Single unified event subscriber (lark-cli only allows one per app)
    event_task = asyncio.create_task(
        listen_all_events(
            on_meeting_end=handle_meeting_end_event,
            on_dm_message=handle_user_message,
            on_card_action=handle_card_action,
        )
    )
    logger.info("Unified event dispatcher started (meeting.end + im.message)")

    # Auto-trigger index rebuild if vector search is enabled and index is cold
    if settings.VECTOR_SEARCH_ENABLED and settings.DOUBAO_EMBEDDING_MODEL:
        import app.persistence as _db
        if _db.doc_index_count() == 0:
            from app.workflow_premeet.wiki_indexer import rebuild_index
            asyncio.create_task(rebuild_index())
            logger.info("Vector index is empty — triggered background rebuild")

    yield

    scheduler.shutdown(wait=False)
    event_task.cancel()
    try:
        await event_task
    except asyncio.CancelledError:
        pass
    logger.info("Shutdown complete")


app = FastAPI(title="Feishu Meeting Automation", lifespan=lifespan)


# ── Health / status ──────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/status")
async def status() -> dict:
    import app.persistence as _db
    return {
        "pushed_events": sorted(_pushed_events),
        "pushed_events_count": len(_pushed_events),
        "qa_context_count": context_store.size(),
        "premeet_push_minutes": settings.PREMEET_PUSH_MINUTES,
        "premeet_poll_interval": settings.PREMEET_POLL_INTERVAL,
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.LLM_MODEL,
        "vector_search_enabled": settings.VECTOR_SEARCH_ENABLED,
        "doc_index_count": _db.doc_index_count(),
    }


# ── Debug / manual trigger endpoints ─────────────────────────────────────────

class PreMeetTriggerRequest(BaseModel):
    event_id: str
    title: str
    description: str = ""
    attendee_open_ids: list[str] = []


@app.post("/debug/trigger-premeet")
async def trigger_premeet(req: PreMeetTriggerRequest) -> dict:
    """
    Manually trigger the pre-meeting pipeline for a synthetic event.
    Uses the same pipeline as the scheduler (brief card format).
    """
    from datetime import datetime, timezone, timedelta
    from app.models import CalendarEvent

    event = CalendarEvent(
        event_id=req.event_id,
        title=req.title,
        description=req.description,
        start_time=datetime.now(tz=timezone.utc) + timedelta(minutes=5),
        end_time=datetime.now(tz=timezone.utc) + timedelta(minutes=65),
        attendee_open_ids=req.attendee_open_ids,
        organizer_open_id="",
    )

    await _run_premeet_pipeline(event)
    return {"status": "triggered", "event_id": req.event_id, "attendees": req.attendee_open_ids}


class PostMeetTriggerRequest(BaseModel):
    meeting_id: str
    dry_run: bool = True


@app.get("/debug/qa-log")
async def qa_log_view(limit: int = 50) -> dict:
    """View recent Q&A records with feedback and judge results."""
    from app.workflow_qa.qa_log import read_qa_records
    records = read_qa_records(limit=limit)
    total = len(records)
    feedback_count = sum(1 for r in records if r.get("feedback"))
    good_count = sum(1 for r in records if r.get("feedback") == "good")
    judged_count = sum(1 for r in records if r.get("judge_score") is not None)
    avg_score = (
        sum(r.get("judge_score", 0) for r in records if r.get("judge_score"))
        / judged_count
        if judged_count else 0
    )
    path_dist: dict[str, int] = {}
    for r in records:
        p = r.get("path", "unknown")
        path_dist[p] = path_dist.get(p, 0) + 1
    return {
        "total": total,
        "feedback_count": feedback_count,
        "satisfaction_rate": round(good_count / feedback_count, 2) if feedback_count else None,
        "judged_count": judged_count,
        "avg_judge_score": round(avg_score, 2) if judged_count else None,
        "path_distribution": path_dist,
        "records": records,
    }


@app.post("/debug/qa-evaluate")
async def qa_evaluate(limit: int = 20) -> dict:
    """Run LLM-as-Judge on recent unevaluated Q&A records."""
    from app.workflow_qa.qa_log import run_batch_evaluation
    return await run_batch_evaluation(limit=limit)


@app.post("/admin/rebuild-index")
async def rebuild_index_api(max_docs: int = 200, recent_days: int = 90) -> dict:
    """
    Manually trigger offline vector index rebuild (runs in background, returns immediately).
    Set max_docs and recent_days to control scope.
    """
    from app.workflow_premeet.wiki_indexer import rebuild_index
    asyncio.create_task(rebuild_index(max_docs=max_docs, recent_days=recent_days))
    return {"status": "started", "max_docs": max_docs, "recent_days": recent_days}


@app.post("/debug/trigger-postmeet")
async def trigger_postmeet(req: PostMeetTriggerRequest) -> dict:
    """
    Manually trigger the post-meeting pipeline for a real meeting ID.
    dry_run=True shows action items without creating tasks.
    """
    return await run_postmeet_pipeline_for_meeting(req.meeting_id, dry_run=req.dry_run)

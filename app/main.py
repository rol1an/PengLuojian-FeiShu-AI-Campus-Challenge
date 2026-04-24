import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from pydantic import BaseModel

from app.config import settings
from app.workflow_premeet.calendar_service import get_upcoming_events
from app.workflow_premeet.wiki_service import generate_keywords, search_wiki
from app.workflow_premeet.push_service import push_knowledge_to_participants
from app.workflow_premeet.scheduler import create_scheduler, _pushed_events
from app.workflow_postmeet.event_listener import listen_for_meeting_end
from app.workflow_postmeet.pipeline import handle_meeting_end_event, run_postmeet_pipeline_for_meeting

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

    # Start post-meeting event listener as background task
    event_task = asyncio.create_task(
        listen_for_meeting_end(on_meeting_end=handle_meeting_end_event)
    )
    logger.info("Post-meeting event listener started")

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
    return {
        "pushed_events_count": len(_pushed_events),
        "premeet_push_minutes": settings.PREMEET_PUSH_MINUTES,
        "premeet_poll_interval": settings.PREMEET_POLL_INTERVAL,
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.LLM_MODEL,
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
    Useful for testing the keyword extraction, wiki search, and card push.
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

    keywords = await generate_keywords(event.title, event.description)
    docs = await search_wiki(keywords)

    results: dict = {
        "keywords": keywords,
        "wiki_docs_found": len(docs),
        "docs": [{"title": d.title, "url": d.url} for d in docs],
        "push_results": {},
    }

    if req.attendee_open_ids:
        results["push_results"] = await push_knowledge_to_participants(event, docs)

    return results


class PostMeetTriggerRequest(BaseModel):
    meeting_id: str
    dry_run: bool = True


@app.post("/debug/trigger-postmeet")
async def trigger_postmeet(req: PostMeetTriggerRequest) -> dict:
    """
    Manually trigger the post-meeting pipeline for a real meeting ID.
    dry_run=True shows action items without creating tasks.
    """
    return await run_postmeet_pipeline_for_meeting(req.meeting_id, dry_run=req.dry_run)

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.models import CalendarEvent
from app.workflow_premeet.calendar_service import get_upcoming_events
from app.workflow_premeet.wiki_service import generate_keywords, search_wiki
from app.workflow_premeet.push_service import push_knowledge_to_participants

logger = logging.getLogger(__name__)

# In-memory set of already-pushed (event_id, push_minutes) keys — prevents duplicate pushes
_pushed_events: set[str] = set()


def _push_key(event_id: str) -> str:
    return f"{event_id}:{settings.PREMEET_PUSH_MINUTES}"


async def premeet_check_job() -> None:
    """
    Poll calendar for upcoming events and push knowledge N minutes before start.
    Runs every PREMEET_POLL_INTERVAL seconds.
    """
    now = datetime.now(tz=timezone.utc)
    push_threshold = timedelta(minutes=settings.PREMEET_PUSH_MINUTES)

    try:
        events = await get_upcoming_events()
    except Exception as e:
        logger.error("Failed to fetch calendar events: %s", e)
        return

    for event in events:
        key = _push_key(event.event_id)
        time_until_start = event.start_time - now

        if timedelta(0) < time_until_start <= push_threshold and key not in _pushed_events:
            _pushed_events.add(key)
            logger.info("Triggering pre-meeting push for: %s", event.title)
            asyncio.create_task(_run_premeet_pipeline(event))


async def _run_premeet_pipeline(event: CalendarEvent) -> None:
    """Full pipeline: keywords → wiki search → push cards."""
    try:
        keywords = await generate_keywords(event.title, event.description)
        logger.info("Keywords for '%s': %s", event.title, keywords)

        docs = await search_wiki(keywords, event.title, event.description)
        logger.info("Found %d wiki docs for '%s'", len(docs), event.title)

        if not event.attendee_open_ids:
            logger.warning("No attendees found for event %s", event.event_id)
            return

        await push_knowledge_to_participants(event, docs)
    except Exception as e:
        logger.error("Pre-meeting push pipeline failed for %s: %s", event.event_id, e)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        premeet_check_job,
        trigger=IntervalTrigger(seconds=settings.PREMEET_POLL_INTERVAL),
        id="premeet_check",
        replace_existing=True,
        max_instances=1,
    )
    return scheduler

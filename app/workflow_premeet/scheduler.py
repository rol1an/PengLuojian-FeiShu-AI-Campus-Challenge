import asyncio
import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.models import CalendarEvent
from app.workflow_premeet.calendar_service import get_upcoming_events
from app.workflow_premeet.wiki_service import analyze_meeting, search_wiki
from app.workflow_premeet.push_service import push_knowledge_to_participants, push_card_payload
from app.workflow_premeet.doc_enricher import enrich_and_repush, enrich_docs_inplace
from app.workflow_premeet.im_context_service import (
    get_dm_messages,
    get_group_messages,
)
from app.workflow_premeet.context_synthesizer import synthesize_brief
from app.workflow_premeet.card_builder import build_brief_card
import app.persistence as db

logger = logging.getLogger(__name__)

# In-memory dict of already-pushed keys → meeting start_time, backed by SQLite for persistence.
_pushed_events: dict[str, datetime] = {}

_PUSHED_EVENTS_TTL = timedelta(hours=2)  # clean up keys this long after meeting start


def _load_pushed_events() -> None:
    """Populate _pushed_events from DB on startup (skips already-expired keys)."""
    db.init_db()
    now = datetime.now(tz=timezone.utc)
    raw = db.pushed_events_load()
    expired: list[str] = []
    for key, start_iso in raw.items():
        try:
            start_time = datetime.fromisoformat(start_iso)
        except ValueError:
            expired.append(key)
            continue
        if now - start_time > _PUSHED_EVENTS_TTL:
            expired.append(key)
        else:
            _pushed_events[key] = start_time
    db.pushed_events_delete(expired)
    if _pushed_events:
        logger.info("Loaded %d pushed_events from DB", len(_pushed_events))


_load_pushed_events()


def _push_key(event_id: str) -> str:
    return f"{event_id}:{settings.PREMEET_PUSH_MINUTES}"


def _gc_pushed_events(now: datetime) -> None:
    expired = [k for k, start in _pushed_events.items() if now - start > _PUSHED_EVENTS_TTL]
    for k in expired:
        del _pushed_events[k]
    db.pushed_events_delete(expired)
    if expired:
        logger.debug("GC: removed %d expired push keys", len(expired))


async def premeet_check_job() -> None:
    """
    Poll calendar for upcoming events and push knowledge N minutes before start.
    Runs every PREMEET_POLL_INTERVAL seconds.
    """
    now = datetime.now(tz=timezone.utc)
    push_threshold = timedelta(minutes=settings.PREMEET_PUSH_MINUTES)

    _gc_pushed_events(now)

    try:
        events = await get_upcoming_events()
    except Exception as e:
        logger.error("Failed to fetch calendar events: %s", e)
        return

    for event in events:
        key = _push_key(event.event_id)
        time_until_start = event.start_time - now

        if timedelta(0) < time_until_start <= push_threshold and key not in _pushed_events:
            _pushed_events[key] = event.start_time
            db.pushed_events_put(key, event.start_time.isoformat())
            logger.info("Triggering pre-meeting push for: %s", event.title)
            asyncio.create_task(_run_premeet_pipeline(event))


async def _run_premeet_pipeline(event: CalendarEvent) -> None:
    """Full pipeline: meeting analysis → IM context → wiki search → enrich → synthesize → push brief card."""
    try:
        meeting_analysis = await analyze_meeting(event.title, event.description)
        keywords = meeting_analysis["doc_search_queries"]
        im_focus = meeting_analysis["im_focus"]
        logger.info("Meeting analysis for '%s': %s", event.title, meeting_analysis)

        if not event.attendee_open_ids:
            logger.warning("No attendees found for event %s", event.event_id)
            return

        # Fetch IM context in parallel (all fail silently)
        chat_id = event.bound_chat_id
        logger.info("IM group chat for '%s': chat_id=%r", event.title, chat_id or "(none, will keyword-search)")
        name_map = event.attendee_names  # open_id → display_name
        # DM targets: organizer + all other attendees (deduped, exclude self)
        dm_targets = list({
            uid for uid in [event.organizer_open_id] + event.attendee_open_ids
            if uid and uid != settings.MY_OPEN_ID
        })
        dm_tasks = [get_dm_messages(uid, target_name=name_map.get(uid, "")) for uid in dm_targets]
        dm_results = await asyncio.gather(
            *dm_tasks,
            get_group_messages(chat_id, name_map=name_map, keywords=keywords),
        )
        group_msgs = dm_results[-1]
        # Merge DM messages from all targets, deduplicate by content+timestamp, keep newest-first
        seen_dm: set[str] = set()
        dm_msgs: list = []
        for msgs in dm_results[:-1]:
            for m in msgs:
                key = f"{m.timestamp.isoformat()}:{m.content[:50]}"
                if key not in seen_dm:
                    seen_dm.add(key)
                    dm_msgs.append(m)
        dm_msgs.sort(key=lambda m: m.timestamp, reverse=True)
        dm_msgs = dm_msgs[:settings.IM_CONTEXT_MAX_MESSAGES]
        logger.info(
            "IM context for '%s': %d DM msgs (%d targets), %d group msgs",
            event.title, len(dm_msgs), len(dm_targets), len(group_msgs),
        )

        docs = await search_wiki(
            keywords,
            event.title,
            event.description,
            attendee_open_ids=event.attendee_open_ids,
            organizer_open_id=event.organizer_open_id,
        )
        logger.info("Found %d wiki docs for '%s'", len(docs), event.title)
        for i, d in enumerate(docs[:3], 1):
            logger.info("PUSH_LOG top%d: '%s' (node=%s)", i, d.title, d.node_token)

        # Enrich top 3 docs in-place (fills why_relevant, conclusions, questions)
        await enrich_docs_inplace(docs[:3], event.title)

        # Synthesize 4-source brief and push the new card format
        brief = await synthesize_brief(event, dm_msgs, group_msgs, docs, im_focus=im_focus)
        logger.info("PUSH_LOG brief for '%s': one_line=%s, bullets=%d", event.title, brief.one_line, len(brief.context_bullets))
        for b in brief.context_bullets:
            logger.info("PUSH_LOG bullet: [%s] %s", b.source_label, b.text)
        payload = build_brief_card(event, brief)
        await push_card_payload(payload, event.attendee_open_ids, event.event_id, label="brief")

        # Store meeting context so attendees can ask follow-up questions
        from app.workflow_qa.context_store import context_store
        for open_id in event.attendee_open_ids:
            context_store.put(open_id, event, brief)
        logger.info(
            "Stored meeting context for %d attendees (event=%s)",
            len(event.attendee_open_ids), event.event_id,
        )

    except Exception as e:
        logger.error("Pre-meeting push pipeline failed for %s: %s", event.event_id, e)


async def _empty_list() -> list:
    return []




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

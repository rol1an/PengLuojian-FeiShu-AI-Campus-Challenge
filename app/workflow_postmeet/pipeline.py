import asyncio
import logging

from app.workflow_postmeet.minutes_service import get_meeting_record
from app.workflow_postmeet.action_extractor import extract_action_items
from app.workflow_postmeet.wiki_linker import enrich_with_wiki_links
from app.workflow_postmeet.task_service import create_task_for_action_item

logger = logging.getLogger(__name__)


async def handle_meeting_end_event(event_data: dict) -> None:
    """Full post-meeting pipeline triggered by vc.meeting.end event. Retries once on failure."""
    event_body = event_data.get("event", {})
    meeting_id = event_body.get("meeting_id")

    if not meeting_id:
        logger.warning("meeting.end event has no meeting_id: %s", event_data)
        return

    for attempt in range(2):
        try:
            await _run_pipeline(meeting_id)
            return
        except Exception as e:
            if attempt == 0:
                logger.warning("Pipeline failed for %s, retrying in 30s: %s", meeting_id, e)
                await asyncio.sleep(30)
            else:
                logger.error("Pipeline failed after retry for %s: %s", meeting_id, e)


async def _run_pipeline(meeting_id: str) -> None:
    """Core pipeline logic (extracted for retry wrapping)."""
    logger.info("Post-meeting pipeline starting for meeting_id=%s", meeting_id)

    record = await get_meeting_record(meeting_id=meeting_id)
    if not record:
        logger.warning("Could not fetch meeting record for %s", meeting_id)
        return

    if not record.transcript and not record.ai_summary:
        logger.warning("Meeting %s has no transcript or summary, skipping", meeting_id)
        return

    items = await extract_action_items(record.transcript, record.ai_summary)
    logger.info("Extracted %d action items from meeting '%s'", len(items), record.title)

    if not items:
        return

    items = await enrich_with_wiki_links(
        items,
        meeting_title=record.title,
        meeting_summary=record.ai_summary,
    )

    created = 0
    for item in items:
        task_id = await create_task_for_action_item(
            item,
            meeting_title=record.title,
            meeting_end_time=record.end_time,
            name_to_open_id=record.name_to_open_id,
        )
        if task_id:
            created += 1

    logger.info("Created %d/%d tasks for meeting '%s'", created, len(items), record.title)


async def run_postmeet_pipeline_for_meeting(
    meeting_id: str, dry_run: bool = False
) -> dict:
    """
    Manually trigger the post-meeting pipeline for a specific meeting.
    Used by the /debug/trigger-postmeet endpoint.
    Returns a summary dict.
    """
    record = await get_meeting_record(meeting_id=meeting_id)
    if not record:
        return {"error": f"Could not fetch meeting record for {meeting_id}"}

    items = await extract_action_items(record.transcript, record.ai_summary)
    items = await enrich_with_wiki_links(
        items,
        meeting_title=record.title,
        meeting_summary=record.ai_summary,
    )

    if dry_run:
        return {
            "meeting_title": record.title,
            "action_items": [
                {
                    "summary": it.summary,
                    "assignee_name": it.assignee_name,
                    "due_hint": it.due_hint,
                    "wiki_links": [
                        {
                            "title": d.title,
                            "url": d.url,
                            "task_background": d.task_background,
                            "how_to_solve": d.how_to_solve,
                            "related_parties": d.related_parties,
                        }
                        for d in it.wiki_links
                    ],
                }
                for it in items
            ],
        }

    created_ids: list[str] = []
    for item in items:
        task_id = await create_task_for_action_item(
            item,
            meeting_title=record.title,
            meeting_end_time=record.end_time,
            name_to_open_id=record.name_to_open_id,
        )
        if task_id:
            created_ids.append(task_id)

    return {
        "meeting_title": record.title,
        "action_items_found": len(items),
        "tasks_created": len(created_ids),
        "task_ids": created_ids,
    }

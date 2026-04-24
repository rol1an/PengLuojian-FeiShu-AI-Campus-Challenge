import json
import logging

from app.lark import run_lark
from app.models import CalendarEvent, WikiDoc
from app.workflow_premeet.card_builder import build_knowledge_card, build_no_docs_card

logger = logging.getLogger(__name__)


async def push_knowledge_to_participants(
    event: CalendarEvent,
    docs: list[WikiDoc],
) -> dict[str, bool]:
    """
    Send knowledge card to each participant individually via DM.
    Returns {open_id: success} mapping.
    """
    payload = build_knowledge_card(event, docs) if docs else build_no_docs_card(event)
    content_json = json.dumps(payload["card"])
    results: dict[str, bool] = {}

    for open_id in event.attendee_open_ids:
        try:
            await run_lark(
                "im",
                "+messages-send",
                "--user-id",
                open_id,
                "--msg-type",
                "interactive",
                "--content",
                content_json,
                as_identity="bot",
                no_format=True,
            )
            results[open_id] = True
            logger.info("Pushed knowledge card to %s for event %s", open_id, event.event_id)
        except Exception as e:
            logger.error("Failed to push to %s: %s", open_id, e)
            results[open_id] = False

    return results

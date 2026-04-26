import json
import logging
from datetime import datetime, timezone

from app.config import settings
from app.lark import run_lark
from app.models import CalendarEvent, WikiDoc
from app.workflow_premeet.card_builder import (
    build_knowledge_card,
    build_low_confidence_card,
    build_no_docs_card,
)

logger = logging.getLogger(__name__)


def _confidence_signals(docs: list[WikiDoc]) -> tuple[float, float, float]:
    """Return (top1_score, gap, avg_score) from scored docs."""
    scores = [d.score for d in docs if d.score > 0]
    if not scores:
        return 0.0, 0.0, 0.0
    top1 = scores[0]
    top2 = scores[1] if len(scores) > 1 else 0.0
    avg = sum(scores) / len(scores)
    return top1, top1 - top2, avg


async def push_knowledge_to_participants(
    event: CalendarEvent,
    docs: list[WikiDoc],
) -> dict[str, bool]:
    """
    Layer 4: compute confidence, pick card variant, push to each attendee.
    Returns {open_id: success} mapping.
    """
    if not docs:
        payload = build_no_docs_card(event)
        is_confident = False
        top1_score = gap = avg_score = 0.0
    else:
        top1_score, gap, avg_score = _confidence_signals(docs)
        is_confident = (
            top1_score >= settings.WIKI_CONFIDENCE_THRESHOLD and avg_score >= 5.0
        )
        if is_confident:
            payload = build_knowledge_card(event, docs)
        else:
            payload = build_low_confidence_card(event, docs)

    # Structured push log
    log_record = {
        "time": datetime.now(tz=timezone.utc).isoformat(),
        "event_id": event.event_id,
        "meeting_title": event.title,
        "top1_score": round(top1_score, 2),
        "gap": round(gap, 2),
        "avg_score": round(avg_score, 2),
        "is_confident": is_confident,
        "docs": [{"title": d.title, "score": round(d.score, 2)} for d in docs],
    }
    logger.info("PUSH_LOG %s", json.dumps(log_record, ensure_ascii=False))

    results = await push_card_payload(payload, event.attendee_open_ids, event.event_id)
    return results


async def push_card_payload(
    payload: dict,
    open_ids: list[str],
    event_id: str,
    label: str = "basic",
) -> dict[str, bool]:
    """Send a card payload to each open_id via DM."""
    content_json = json.dumps(payload["card"])
    results: dict[str, bool] = {}

    for open_id in open_ids:
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
            logger.info(
                "Pushed %s card to %s for event %s", label, open_id, event_id
            )
        except Exception as e:
            logger.error("Failed to push %s card to %s: %s", label, open_id, e)
            results[open_id] = False

    return results

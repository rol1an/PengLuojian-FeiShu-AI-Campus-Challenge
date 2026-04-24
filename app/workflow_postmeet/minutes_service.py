import logging
from datetime import datetime, timezone

from app.lark import run_lark
from app.models import MeetingRecord

logger = logging.getLogger(__name__)


async def get_meeting_record(
    meeting_id: str | None = None,
    calendar_event_id: str | None = None,
) -> MeetingRecord | None:
    """Fetch meeting notes and transcript via lark-cli vc +notes."""
    if not meeting_id and not calendar_event_id:
        return None

    flags: list[str] = []
    if meeting_id:
        flags.extend(["--meeting-ids", meeting_id])
    if calendar_event_id:
        flags.extend(["--calendar-event-ids", calendar_event_id])

    try:
        data = await run_lark("vc", "+notes", *flags, as_identity="user")
    except Exception as e:
        logger.error("Failed to fetch meeting notes: %s", e)
        return None

    if not data:
        return None

    # vc +notes may return a list or a single record
    record: dict = data if isinstance(data, dict) else (data[0] if data else {})
    if not record:
        return None

    transcript = _extract_transcript(record)

    return MeetingRecord(
        meeting_id=meeting_id or record.get("meeting_id", ""),
        calendar_event_id=calendar_event_id,
        minute_token=record.get("minute_token"),
        title=record.get("topic", "Unknown Meeting"),
        end_time=datetime.now(tz=timezone.utc),
        participant_open_ids=[p.get("open_id", "") for p in record.get("participants", [])],
        transcript=transcript,
        ai_summary=record.get("summary", ""),
    )


def _extract_transcript(record: dict) -> str:
    """Extract plain-text transcript from the notes artifact."""
    t = record.get("transcript")
    if t:
        if isinstance(t, str):
            return t
        if isinstance(t, list):
            return "\n".join(
                f"{seg.get('speaker', '')}: {seg.get('text', '')}" for seg in t
            )
    content = record.get("content")
    if content:
        return content
    return record.get("ai_summary", "")

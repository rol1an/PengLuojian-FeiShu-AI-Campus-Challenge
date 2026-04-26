import logging
import re
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

    notes = data.get("data", {}).get("notes", [])
    if not notes:
        logger.warning("No notes found for meeting_id=%s", meeting_id)
        return None

    note = notes[0]
    verbatim_token = note.get("verbatim_doc_token", "")
    summary_token = note.get("note_doc_token", "")

    transcript = ""
    ai_summary = ""

    if verbatim_token:
        transcript = await _fetch_doc_text(verbatim_token)
        logger.info("Fetched verbatim transcript (%d chars) from %s", len(transcript), verbatim_token)

    if summary_token:
        ai_summary = await _fetch_doc_text(summary_token)
        logger.info("Fetched AI summary (%d chars) from %s", len(ai_summary), summary_token)

    # Try to get meeting title + organizer name via vc +search
    title, organizer_name = await _fetch_meeting_meta(meeting_id) if meeting_id else ("Unknown Meeting", "")

    # Build open_id → name mapping: map note creator to organizer name
    id_to_name: dict[str, str] = {}
    creator_id = note.get("creator_id", "")
    if creator_id and organizer_name:
        id_to_name[creator_id] = organizer_name
    elif creator_id:
        name = await _lookup_user_name(creator_id)
        if name:
            id_to_name[creator_id] = name

    # Replace <mention-user id="ou_xxx"/> with real names in transcript
    if id_to_name and transcript:
        transcript = _resolve_mention_users(transcript, id_to_name)

    return MeetingRecord(
        meeting_id=meeting_id or note.get("meeting_id", ""),
        calendar_event_id=calendar_event_id,
        minute_token=summary_token or None,
        title=title,
        end_time=datetime.now(tz=timezone.utc),
        participant_open_ids=list(id_to_name.keys()),
        transcript=transcript,
        ai_summary=ai_summary,
    )


async def _fetch_doc_text(doc_token: str) -> str:
    """Fetch markdown content of a doc via lark-cli docs +fetch."""
    try:
        data = await run_lark(
            "docs", "+fetch",
            "--doc", doc_token,
            as_identity="user",
            timeout=20,
        )
        return data.get("data", {}).get("markdown", "")
    except Exception as e:
        logger.warning("Failed to fetch doc %s: %s", doc_token, e)
        return ""


async def _fetch_meeting_meta(meeting_id: str) -> tuple[str, str]:
    """Get meeting title and organizer name from vc +search."""
    try:
        from datetime import date, timedelta
        start = (date.today() - timedelta(days=7)).isoformat()
        data = await run_lark(
            "vc", "+search",
            "--start", start,
            as_identity="user",
            timeout=15,
        )
        for item in data.get("data", {}).get("items", []):
            if item.get("id") == meeting_id:
                display = item.get("display_info", "")
                title = display.split("\n")[0] if display else "Unknown Meeting"
                org_match = re.search(r"组织者[：:]\s*(\S+)", display)
                organizer_name = org_match.group(1) if org_match else ""
                return title, organizer_name
    except Exception:
        pass
    return "Unknown Meeting", ""


async def _lookup_user_name(open_id: str) -> str:
    """Look up a user's name by open_id."""
    try:
        data = await run_lark(
            "contact", "users", "get",
            "--user-id", open_id,
            "--user-id-type", "open_id",
            as_identity="user",
            timeout=10,
        )
        return data.get("data", {}).get("user", {}).get("name", "")
    except Exception:
        return ""


def _resolve_mention_users(text: str, id_to_name: dict[str, str]) -> str:
    """Replace <mention-user id="ou_xxx"/> tags with real names."""
    def replacer(m: re.Match) -> str:
        uid = m.group(1)
        return id_to_name.get(uid, uid)
    return re.sub(r'<mention-user id="([^"]+)"/>', replacer, text)

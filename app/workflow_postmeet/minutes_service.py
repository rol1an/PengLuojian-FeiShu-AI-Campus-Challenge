import asyncio
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

    # Fetch full meeting detail: title, real end_time, all participant names
    title = "Unknown Meeting"
    end_time = datetime.now(tz=timezone.utc)
    id_to_name: dict[str, str] = {}

    if meeting_id:
        title, end_time, id_to_name = await _fetch_meeting_detail(meeting_id)

    # Fallback: map only the note creator via vc +search
    if not id_to_name:
        fallback_title, organizer_name = await _fetch_meeting_meta_fallback(meeting_id) if meeting_id else ("Unknown Meeting", "")
        if fallback_title != "Unknown Meeting":
            title = fallback_title
        creator_id = note.get("creator_id", "")
        if creator_id and organizer_name:
            id_to_name[creator_id] = organizer_name
        elif creator_id:
            name = await _lookup_user_name(creator_id)
            if name:
                id_to_name[creator_id] = name

    name_to_open_id = {v: k for k, v in id_to_name.items()}

    # Replace <mention-user id="ou_xxx"/> with real names in transcript
    if id_to_name and transcript:
        transcript = _resolve_mention_users(transcript, id_to_name)

    return MeetingRecord(
        meeting_id=meeting_id or note.get("meeting_id", ""),
        calendar_event_id=calendar_event_id,
        minute_token=summary_token or None,
        title=title,
        end_time=end_time,
        participant_open_ids=list(id_to_name.keys()),
        transcript=transcript,
        ai_summary=ai_summary,
        name_to_open_id=name_to_open_id,
    )


async def _fetch_meeting_detail(meeting_id: str) -> tuple[str, datetime, dict[str, str]]:
    """
    Get meeting title, real end_time, and full participant name mapping
    via vc meeting get with with_participants=true.
    Returns (title, end_time, id_to_name).
    """
    title = "Unknown Meeting"
    end_time = datetime.now(tz=timezone.utc)
    id_to_name: dict[str, str] = {}

    try:
        import json as _json
        params = _json.dumps({"meeting_id": meeting_id, "with_participants": "true", "user_id_type": "open_id"})
        data = await run_lark(
            "vc", "meeting", "get",
            "--params", params,
            as_identity="user",
            timeout=15,
        )
        meeting = data.get("data", {}).get("meeting", {})

        topic = meeting.get("topic", "")
        if topic:
            title = topic

        raw_end = meeting.get("end_time", "")
        if raw_end:
            try:
                end_time = datetime.fromtimestamp(int(raw_end), tz=timezone.utc)
            except (ValueError, TypeError):
                pass

        participants = meeting.get("participants", [])
        if not isinstance(participants, list):
            participants = []

        open_ids = [p.get("id", "") for p in participants if p.get("id")]
        if open_ids:
            names = await asyncio.gather(
                *[_lookup_user_name(oid) for oid in open_ids],
                return_exceptions=True,
            )
            for oid, name in zip(open_ids, names):
                if isinstance(name, str) and name:
                    id_to_name[oid] = name

        logger.info(
            "Meeting detail: title='%s', end=%s, participants=%d",
            title, end_time.isoformat(), len(id_to_name),
        )
    except Exception as e:
        logger.warning("Failed to fetch meeting detail for %s: %s", meeting_id, e)

    return title, end_time, id_to_name


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


async def _fetch_meeting_meta_fallback(meeting_id: str) -> tuple[str, str]:
    """Fallback: get meeting title and organizer name from vc +search."""
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
            "contact", "+get-user",
            "--user-id", open_id,
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

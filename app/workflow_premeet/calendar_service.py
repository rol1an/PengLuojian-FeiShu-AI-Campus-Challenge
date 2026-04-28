import json
import logging
import time
from datetime import datetime, timezone

from app.config import settings
from app.lark import run_lark
from app.models import CalendarEvent

logger = logging.getLogger(__name__)


async def get_upcoming_events(lookahead_hours: int | None = None) -> list[CalendarEvent]:
    """Fetch events starting in the next lookahead_hours window."""
    hours = lookahead_hours or settings.PREMEET_LOOKAHEAD_HOURS
    now = int(time.time())
    end = now + hours * 3600

    data = await run_lark(
        "calendar",
        "events",
        "instance_view",
        "--params",
        json.dumps(
            {
                "calendar_id": settings.CALENDAR_ID,
                "start_time": str(now),
                "end_time": str(end),
                "user_id_type": "open_id",
            }
        ),
        as_identity="user",
    )

    events: list[CalendarEvent] = []
    for item in data.get("data", {}).get("items", []):
        attendee_ids, attendee_names = await _get_attendee_info(item["event_id"])
        events.append(
            CalendarEvent(
                event_id=item["event_id"],
                title=item.get("summary", ""),
                description=item.get("description", ""),
                start_time=datetime.fromtimestamp(
                    int(item["start_time"]["timestamp"]), tz=timezone.utc
                ),
                end_time=datetime.fromtimestamp(
                    int(item["end_time"]["timestamp"]), tz=timezone.utc
                ),
                attendee_open_ids=attendee_ids,
                organizer_open_id=item.get("event_organizer", {}).get("user_id", ""),
                attendee_names=attendee_names,
            )
        )
    return events


async def _get_attendee_info(event_id: str) -> tuple[list[str], dict[str, str]]:
    """Return (open_id list, open_id→display_name map) for a calendar event."""
    try:
        data = await run_lark(
            "calendar",
            "event.attendees",
            "list",
            "--params",
            json.dumps(
                {
                    "calendar_id": settings.CALENDAR_ID,
                    "event_id": event_id,
                    "user_id_type": "open_id",
                }
            ),
            as_identity="user",
        )
        ids: list[str] = []
        names: dict[str, str] = {}
        for a in data.get("data", {}).get("items", []):
            if a.get("type") == "user" and a.get("user_id"):
                uid = a["user_id"]
                ids.append(uid)
                if a.get("display_name"):
                    names[uid] = a["display_name"]
        return ids, names
    except Exception as e:
        logger.warning("Failed to fetch attendees for event %s: %s", event_id, e)
        return [], {}

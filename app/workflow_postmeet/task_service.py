import json
import logging
from datetime import datetime, timezone, timedelta

from app.config import settings
from app.lark import run_lark
from app.models import ActionItem

logger = logging.getLogger(__name__)


async def resolve_assignee(name: str) -> str | None:
    """Look up open_id by name using contact search."""
    if not name.strip():
        return None
    try:
        data = await run_lark(
            "contact",
            "+search-user",
            "--query",
            name,
            as_identity="user",
        )
        users = data.get("data", {}).get("users", [])
        if users:
            return users[0].get("open_id")
    except Exception as e:
        logger.warning("Contact lookup failed for '%s': %s", name, e)
    return None


async def create_task_for_action_item(
    item: ActionItem,
    meeting_title: str,
    meeting_end_time: datetime | None = None,
    tasklist_id: str | None = None,
) -> str | None:
    """
    Create a Feishu task for an action item.
    Returns the task_id on success, None on failure.
    """
    if item.assignee_open_id is None and item.assignee_name:
        item.assignee_open_id = await resolve_assignee(item.assignee_name)

    desc_parts = [f"来自会议：**{meeting_title}**", "", f"背景：{item.context}"]
    if item.wiki_links:
        desc_parts.append("\n相关知识文档：")
        for doc in item.wiki_links:
            desc_parts.append(f"- [{doc.title}]({doc.url})")
    description = "\n".join(desc_parts)

    # Build start / due timestamps
    now = datetime.now(tz=timezone.utc)
    start_date = _parse_time_hint(item.start_hint, now) if item.start_hint else None
    due_date = _parse_time_hint(item.due_hint, now) if item.due_hint else None

    # Start fallback: meeting end time
    if start_date is None:
        start_date = (meeting_end_time or now).date()

    # Build --data payload (required for start field)
    task_data: dict = {
        "summary": item.summary,
        "description": description,
        "start": {
            "timestamp": str(int(datetime(start_date.year, start_date.month, start_date.day,
                                          tzinfo=timezone.utc).timestamp() * 1000)),
            "is_all_day": True,
        },
    }
    if due_date is not None:
        task_data["due"] = {
            "timestamp": str(int(datetime(due_date.year, due_date.month, due_date.day,
                                          tzinfo=timezone.utc).timestamp() * 1000)),
            "is_all_day": True,
        }
    if item.assignee_open_id:
        task_data["members"] = [{"id": item.assignee_open_id, "type": "user", "role": "assignee"}]

    flags = ["--data", json.dumps(task_data)]
    if tasklist_id:
        flags.extend(["--tasklist-id", tasklist_id])

    try:
        result = await run_lark("task", "tasks", "create", *flags, as_identity="user")
        task_id = result.get("data", {}).get("task", {}).get("guid") or result.get("data", {}).get("guid")
        logger.info("Created task '%s' → %s (start=%s due=%s)", item.summary[:50], task_id, start_date, due_date)
        return task_id
    except Exception as e:
        logger.error("Task creation failed for '%s': %s", item.summary[:50], e)
        return None


def _parse_time_hint(hint: str, reference: datetime):
    """Parse a natural language time hint to a date. Returns None if hint is empty."""
    from datetime import date
    if not hint or not hint.strip():
        return None
    hint_lower = hint.lower()
    now = reference

    if "today" in hint_lower or "今天" in hint_lower or "eod" in hint_lower:
        return now.date()
    elif "tomorrow" in hint_lower or "明天" in hint_lower:
        return (now + timedelta(days=1)).date()
    elif "后天" in hint_lower:
        return (now + timedelta(days=2)).date()
    elif "friday" in hint_lower or "周五" in hint_lower or "本周五" in hint_lower:
        days = (4 - now.weekday()) % 7
        return (now + timedelta(days=days or 7)).date()
    elif "monday" in hint_lower or "下周一" in hint_lower:
        days = (7 - now.weekday()) % 7 + 1
        return (now + timedelta(days=days)).date()
    elif "next week" in hint_lower or "下周" in hint_lower:
        return (now + timedelta(weeks=1)).date()
    elif "two week" in hint_lower or "2 week" in hint_lower or "两周" in hint_lower:
        return (now + timedelta(weeks=2)).date()
    elif "month" in hint_lower or "下个月" in hint_lower:
        return (now + timedelta(days=30)).date()
    return None

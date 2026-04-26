import json
import logging
from datetime import datetime, timezone, timedelta

from app.config import settings
from app.lark import run_lark
from app.models import ActionItem

logger = logging.getLogger(__name__)


_HONORIFICS = ("哥", "姐", "总", "老师", "同学")


def _strip_honorific(name: str) -> str:
    """Strip common honorific suffixes (one character)."""
    for h in _HONORIFICS:
        if name.endswith(h) and len(name) > 1:
            return name[:-len(h)]
    return name


async def resolve_assignee(
    name: str,
    name_to_open_id: dict[str, str] | None = None,
) -> str | None:
    """
    Look up open_id for assignee_name.
    Resolution order:
    1. Exact match in name_to_open_id (participant table)
    2. Honorific-stripped exact match
    3. Fuzzy match (startswith/in) — return None if multiple matches (ambiguous)
    4. Fallback: contact +search-user — return None if >1 result and none in participants
    """
    if not name.strip():
        return None

    participant_open_ids = set((name_to_open_id or {}).values())

    if name_to_open_id:
        # 1. Exact match
        if name in name_to_open_id:
            return name_to_open_id[name]

        # 2. Honorific-stripped exact match
        stripped = _strip_honorific(name)
        if stripped != name and stripped in name_to_open_id:
            return name_to_open_id[stripped]

        # 3. Fuzzy match — bail on ambiguity
        candidates = [
            oid for n, oid in name_to_open_id.items()
            if n.startswith(stripped) or stripped in n
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            logger.warning("Ambiguous assignee '%s' matches %d participants, leaving empty", name, len(candidates))
            return None

    # 4. Fallback: global contact search
    try:
        data = await run_lark(
            "contact",
            "+search-user",
            "--query",
            _strip_honorific(name),
            as_identity="user",
        )
        users = data.get("data", {}).get("users", [])
        if not users:
            return None
        # If exactly one result, use it
        if len(users) == 1:
            return users[0].get("open_id")
        # Multiple results: only accept if exactly one is a known participant
        participant_matches = [u for u in users if u.get("open_id") in participant_open_ids]
        if len(participant_matches) == 1:
            return participant_matches[0].get("open_id")
        logger.warning("Contact search for '%s' returned %d results, cannot resolve", name, len(users))
    except Exception as e:
        logger.warning("Contact lookup failed for '%s': %s", name, e)
    return None


async def create_task_for_action_item(
    item: ActionItem,
    meeting_title: str,
    meeting_end_time: datetime | None = None,
    name_to_open_id: dict[str, str] | None = None,
    tasklist_id: str | None = None,
) -> str | None:
    """
    Create a Feishu task for an action item.
    Returns the task_id on success, None on failure.
    """
    if item.assignee_open_id is None and item.assignee_name:
        item.assignee_open_id = await resolve_assignee(item.assignee_name, name_to_open_id)

    desc_parts = [f"来自会议：**{meeting_title}**", "", f"背景：{item.context}"]
    if item.wiki_links:
        desc_parts.append("\n相关知识文档：")
        for doc in item.wiki_links:
            reason = f"（{doc.link_reason}）" if doc.link_reason else ""
            desc_parts.append(f"- [{doc.title}]({doc.url}){reason}")
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

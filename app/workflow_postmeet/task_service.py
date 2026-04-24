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
        users = data.get("users", [])
        if users:
            return users[0].get("open_id")
    except Exception as e:
        logger.warning("Contact lookup failed for '%s': %s", name, e)
    return None


async def create_task_for_action_item(
    item: ActionItem,
    meeting_title: str,
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

    due_date = _parse_due_hint(item.due_hint)

    flags = [
        "--summary",
        item.summary,
        "--description",
        description,
        "--due",
        due_date,
    ]
    if item.assignee_open_id:
        flags.extend(["--assignee", item.assignee_open_id])
    if tasklist_id:
        flags.extend(["--tasklist-id", tasklist_id])

    try:
        result = await run_lark("task", "+create", *flags, as_identity="user")
        task_id = result.get("data", {}).get("guid") or result.get("guid")
        logger.info("Created task '%s' → %s", item.summary[:50], task_id)
        return task_id
    except Exception as e:
        logger.error("Task creation failed for '%s': %s", item.summary[:50], e)
        return None


def _parse_due_hint(hint: str) -> str:
    """Convert natural language due hint to ISO date string. Falls back to N days from now."""
    hint_lower = hint.lower()
    now = datetime.now(tz=timezone.utc)

    if "today" in hint_lower or "eod" in hint_lower or "今天" in hint_lower:
        due = now
    elif "tomorrow" in hint_lower or "明天" in hint_lower:
        due = now + timedelta(days=1)
    elif "friday" in hint_lower or "周五" in hint_lower or "本周五" in hint_lower:
        days_until_friday = (4 - now.weekday()) % 7
        due = now + timedelta(days=days_until_friday or 7)
    elif "next week" in hint_lower or "下周" in hint_lower:
        due = now + timedelta(weeks=1)
    elif "two week" in hint_lower or "2 week" in hint_lower or "两周" in hint_lower:
        due = now + timedelta(weeks=2)
    else:
        due = now + timedelta(days=settings.TASK_DEFAULT_DUE_DAYS)

    return due.strftime('%Y-%m-%d')

"""IM context service: fetch DM and group chat context for pre-meeting briefs."""
import logging
import re
from datetime import datetime, timezone, timedelta

from app.config import settings
from app.lark import run_lark
from app.models import ChatMessage

_CST = timezone(timedelta(hours=8))

logger = logging.getLogger(__name__)


def extract_chat_id_from_description(description: str) -> str | None:
    """Extract a Feishu group chat ID from a calendar event description."""
    if not description:
        return None
    patterns = [
        r"openChatId=([A-Za-z0-9_-]+)",
        r"chat_id=([A-Za-z0-9_-]+)",
        r"/chat/open\?[^\"'\s]*openChatId=([A-Za-z0-9_-]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, description)
        if m:
            return m.group(1)
    return None


async def get_dm_messages(
    target_open_id: str,
    target_name: str = "",
    days: int | None = None,
) -> list[ChatMessage]:
    """Fetch recent DM messages between the current user and target_open_id."""
    if not target_open_id:
        return []
    days = days or settings.IM_CONTEXT_DAYS
    start_iso = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
    try:
        data = await run_lark(
            "im", "+chat-messages-list",
            "--user-id", target_open_id,
            "--start", start_iso,
            "--page-size", "50",
            "--sort", "desc",
            as_identity="user",
            timeout=15,
        )
        items = data.get("data", {}).get("messages", [])
        messages: list[ChatMessage] = []
        for item in items:
            if item.get("deleted"):
                continue
            content = item.get("content", "").strip()
            if not content:
                continue
            sender = item.get("sender", {})
            ts_str = item.get("create_time", "")
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M").replace(tzinfo=_CST)
            except (ValueError, TypeError):
                ts = datetime.now(tz=timezone.utc)
            messages.append(
                ChatMessage(
                    sender_open_id=sender.get("id", ""),
                    sender_name=sender.get("name", "") or target_name,
                    content=content,
                    timestamp=ts,
                    chat_id=target_open_id,
                    source="dm",
                    chat_name=target_name,
                )
            )
        messages.sort(key=lambda m: m.timestamp, reverse=True)

        # If target_name looks like an anonymous placeholder, try to find a better name
        # from messages sent by the target (their sender_name may be more readable)
        resolved_name = target_name
        if _is_placeholder_name(target_name):
            for m in messages:
                if m.sender_open_id == target_open_id and m.sender_name and not _is_placeholder_name(m.sender_name):
                    resolved_name = m.sender_name
                    break
        if resolved_name != target_name:
            for m in messages:
                if m.chat_name == target_name:
                    m.chat_name = resolved_name

        return messages[: settings.IM_CONTEXT_MAX_MESSAGES]
    except Exception as e:
        logger.debug("get_dm_messages failed for %s: %s", target_open_id, e)
        return []


def _is_placeholder_name(name: str) -> bool:
    """Detect anonymous/placeholder display names like '用户604098'."""
    if not name:
        return True
    import re
    return bool(re.match(r"^用户\d+$", name))


async def get_group_messages(
    chat_id: str,
    days: int | None = None,
    name_map: dict[str, str] | None = None,
) -> list[ChatMessage]:
    """Fetch recent group chat messages."""
    if not chat_id:
        return []
    days = days or 7
    try:
        messages = await _fetch_recent_messages(chat_id, days, source="group", name_map=name_map or {})
        return messages[: settings.IM_CONTEXT_MAX_MESSAGES]
    except Exception as e:
        logger.debug("get_group_messages failed for chat %s: %s", chat_id, e)
        return []



async def _fetch_recent_messages(
    chat_id: str, days: int, source: str, name_map: dict[str, str] | None = None,
) -> list[ChatMessage]:
    """Fetch messages from a chat within the past N days."""
    start_iso = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
    try:
        data = await run_lark(
            "im", "+chat-messages-list",
            "--chat-id", chat_id,
            "--start", start_iso,
            "--page-size", "50",
            "--sort", "desc",
            as_identity="user",
            timeout=15,
        )
    except Exception as e:
        logger.debug("_fetch_recent_messages failed for %s: %s", chat_id, e)
        return []

    name_map = name_map or {}
    items = data.get("data", {}).get("messages", [])
    messages: list[ChatMessage] = []
    for item in items:
        if item.get("deleted"):
            continue
        content = item.get("content", "").strip()
        if not content:
            continue
        sender = item.get("sender", {})
        sender_id = sender.get("id", "")
        sender_name = sender.get("name", "") or name_map.get(sender_id, "")
        ts_str = item.get("create_time", "")
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M").replace(tzinfo=_CST)
        except (ValueError, TypeError):
            ts = datetime.now(tz=timezone.utc)
        messages.append(
            ChatMessage(
                sender_open_id=sender_id,
                sender_name=sender_name,
                content=content,
                timestamp=ts,
                chat_id=chat_id,
                source=source,
            )
        )
    messages.sort(key=lambda m: m.timestamp, reverse=True)
    return messages




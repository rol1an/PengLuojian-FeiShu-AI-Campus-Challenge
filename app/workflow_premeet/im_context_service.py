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
    except Exception as e:
        err_str = str(e)
        if "231204" in err_str or "b2c" in err_str:
            logger.debug("get_dm_messages: b2c user %s, falling back to messages-search", target_open_id)
            return await _get_dm_messages_via_search(target_open_id, target_name, days)
        logger.debug("get_dm_messages failed for %s: %s", target_open_id, e)
        return []

    items = data.get("data", {}).get("messages", [])
    messages = _parse_message_items(items, target_open_id, target_name, source="dm")
    messages.sort(key=lambda m: m.timestamp, reverse=True)
    _resolve_placeholder_name(messages, target_open_id, target_name)
    return messages[: settings.IM_CONTEXT_MAX_MESSAGES]


async def _get_dm_messages_via_search(
    target_open_id: str,
    target_name: str,
    days: int,
) -> list[ChatMessage]:
    """Fallback for b2c external users: discover chat_id via messages-search, then fetch full conversation."""
    start_cst = (datetime.now(tz=_CST) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    # Step 1: find the p2p chat_id by searching messages sent by target
    try:
        probe = await run_lark(
            "im", "+messages-search",
            "--sender", target_open_id,
            "--start", start_cst,
            "--page-size", "1",
            as_identity="user",
            timeout=15,
        )
    except Exception as e:
        logger.debug("_get_dm_messages_via_search probe failed for %s: %s", target_open_id, e)
        return []

    probe_msgs = probe.get("data", {}).get("messages", [])
    if not probe_msgs:
        logger.debug("_get_dm_messages_via_search: no messages found for %s", target_open_id)
        return []

    chat_id = probe_msgs[0].get("chat_id", "")
    if not chat_id:
        return []

    # Step 2: fetch full bidirectional conversation via chat_id
    try:
        data = await run_lark(
            "im", "+messages-search",
            "--chat-id", chat_id,
            "--start", start_cst,
            "--page-size", "50",
            as_identity="user",
            timeout=15,
        )
    except Exception as e:
        logger.debug("_get_dm_messages_via_search chat fetch failed for %s: %s", chat_id, e)
        return []

    items = data.get("data", {}).get("messages", [])
    messages = _parse_message_items(items, target_open_id, target_name, source="dm")
    messages.sort(key=lambda m: m.timestamp, reverse=True)
    _resolve_placeholder_name(messages, target_open_id, target_name)
    logger.info("_get_dm_messages_via_search: fetched %d msgs for b2c user %s via chat %s", len(messages), target_open_id, chat_id)
    return messages[: settings.IM_CONTEXT_MAX_MESSAGES]


def _parse_message_items(
    items: list,
    target_open_id: str,
    target_name: str,
    source: str,
) -> list[ChatMessage]:
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
                source=source,
                chat_name=target_name,
            )
        )
    return messages


def _resolve_placeholder_name(
    messages: list[ChatMessage],
    target_open_id: str,
    target_name: str,
) -> None:
    """Replace placeholder display names (用户XXXXXX) with real names found in messages."""
    if not _is_placeholder_name(target_name):
        return
    resolved_name = target_name
    for m in messages:
        if m.sender_open_id == target_open_id and m.sender_name and not _is_placeholder_name(m.sender_name):
            resolved_name = m.sender_name
            break
    if resolved_name != target_name:
        for m in messages:
            if m.chat_name == target_name:
                m.chat_name = resolved_name


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




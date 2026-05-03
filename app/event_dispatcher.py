"""
Unified lark event dispatcher.

Runs a SINGLE lark-cli event +subscribe process and routes events to the
appropriate handlers based on event_type.  This is required because lark-cli
only allows one subscriber per app at a time.
"""
import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

from app.lark import stream_lark_events

logger = logging.getLogger(__name__)

OnMeetingEndCallback = Callable[[dict], Awaitable[None]]
OnMessageCallback = Callable[[str, str], Awaitable[None]]

# ── Meeting-end dedup ────────────────────────────────────────────────────────
_MEETING_DEDUP_TTL = 300  # 5 minutes
_seen_meetings: dict[str, float] = {}
_meeting_sem = asyncio.Semaphore(3)

# ── IM message dedup ─────────────────────────────────────────────────────────
_MSG_DEDUP_TTL = 600  # 10 minutes
_seen_messages: dict[str, float] = {}
_msg_sem = asyncio.Semaphore(2)


async def _guarded_meeting(cb: OnMeetingEndCallback, event: dict) -> None:
    async with _meeting_sem:
        await cb(event)


async def _guarded_message(cb: OnMessageCallback, open_id: str, text: str) -> None:
    async with _msg_sem:
        await cb(open_id, text)


def _handle_meeting_end(event: dict, cb: OnMeetingEndCallback) -> None:
    meeting_id = event.get("event", {}).get("meeting_id", "unknown")
    now_ts = time.monotonic()
    if meeting_id in _seen_meetings and now_ts - _seen_meetings[meeting_id] < _MEETING_DEDUP_TTL:
        logger.info("Duplicate meeting.end for %s, skipping", meeting_id)
        return
    _seen_meetings[meeting_id] = now_ts
    logger.info("Meeting ended: %s", meeting_id)
    asyncio.create_task(_guarded_meeting(cb, event))


def _handle_im_message(event: dict, cb: OnMessageCallback) -> None:
    msg = event.get("event", {}).get("message", {})
    sender = event.get("event", {}).get("sender", {})

    if msg.get("chat_type") != "p2p":
        return
    if msg.get("message_type") != "text":
        return

    message_id = msg.get("message_id", "")
    if not message_id:
        return

    now_ts = time.monotonic()
    if message_id in _seen_messages and now_ts - _seen_messages[message_id] < _MSG_DEDUP_TTL:
        logger.debug("Duplicate message %s, skipping", message_id)
        return
    _seen_messages[message_id] = now_ts

    open_id = sender.get("sender_id", {}).get("open_id", "")
    if not open_id:
        return

    try:
        text = json.loads(msg.get("content", "{}")).get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        text = ""
    if not text:
        return

    logger.info("DM from %s: %s", open_id, text[:80])
    asyncio.create_task(_guarded_message(cb, open_id, text))


async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
    """Log lark-cli stderr so connection status / errors are visible."""
    try:
        async for line in proc.stderr:  # type: ignore[union-attr]
            text = line.decode().strip()
            if text:
                logger.info("[lark-cli stderr] %s", text)
    except Exception:
        pass


async def listen_all_events(
    on_meeting_end: OnMeetingEndCallback,
    on_dm_message: OnMessageCallback,
) -> None:
    """
    Single lark-cli subscriber that dispatches to both meeting-end and DM-message
    handlers.  Reconnects automatically on failure.
    """
    while True:
        logger.info("Starting unified lark event subscription...")
        proc = await stream_lark_events(as_identity="bot")
        asyncio.create_task(_drain_stderr(proc))

        try:
            async for line in proc.stdout:  # type: ignore[union-attr]
                raw = line.decode().strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON event line: %s", raw[:100])
                    continue

                event_type = event.get("event_type", "") or event.get("header", {}).get(
                    "event_type", ""
                )

                if "meeting.end" in event_type:
                    _handle_meeting_end(event, on_meeting_end)
                elif "im.message.receive" in event_type:
                    _handle_im_message(event, on_dm_message)

        except Exception as e:
            logger.error("Event dispatcher error: %s", e)
        finally:
            proc.kill()
            logger.warning("Event stream disconnected, reconnecting in 5s...")
            await asyncio.sleep(5)

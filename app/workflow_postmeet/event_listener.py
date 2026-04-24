import asyncio
import json
import logging
from typing import Callable, Awaitable

from app.config import settings
from app.lark import stream_lark_events

logger = logging.getLogger(__name__)

OnMeetingEndCallback = Callable[[dict], Awaitable[None]]


async def listen_for_meeting_end(on_meeting_end: OnMeetingEndCallback) -> None:
    """
    Consume lark-cli event +subscribe NDJSON stream indefinitely.
    Calls on_meeting_end(event_data) when a vc.meeting.end event arrives.
    Auto-reconnects on process failure.
    """
    while True:
        logger.info("Starting lark event subscription...")
        proc = await stream_lark_events(
            "--filter",
            settings.EVENT_FILTER,
            "--compact",
            as_identity="bot",
        )

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
                    meeting_id = event.get("event", {}).get("meeting_id", "unknown")
                    logger.info("Meeting ended: %s", meeting_id)
                    asyncio.create_task(on_meeting_end(event))

        except Exception as e:
            logger.error("Event listener error: %s", e)
        finally:
            proc.kill()
            logger.warning("Event stream disconnected, reconnecting in 5s...")
            await asyncio.sleep(5)

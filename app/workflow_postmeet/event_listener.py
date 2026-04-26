import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

from app.config import settings
from app.lark import stream_lark_events

logger = logging.getLogger(__name__)

OnMeetingEndCallback = Callable[[dict], Awaitable[None]]

_DEDUP_TTL = 300  # seconds — suppress duplicate meeting.end within 5 min
_seen: dict[str, float] = {}  # meeting_id → monotonic timestamp of first seen
_sem = asyncio.Semaphore(3)   # at most 3 concurrent postmeet pipelines


async def _guarded(on_meeting_end: OnMeetingEndCallback, event: dict) -> None:
    async with _sem:
        await on_meeting_end(event)


async def listen_for_meeting_end(on_meeting_end: OnMeetingEndCallback) -> None:
    """
    Consume lark-cli event +subscribe NDJSON stream indefinitely.
    Calls on_meeting_end(event_data) when a vc.meeting.end event arrives.
    - Deduplicates identical meeting_id within 5 minutes.
    - Limits concurrent pipelines to 3 via Semaphore.
    - Auto-reconnects on process failure.
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
                if "meeting.end" not in event_type:
                    continue

                meeting_id = event.get("event", {}).get("meeting_id", "unknown")

                # Dedup: skip if same meeting_id seen within TTL
                now_ts = time.monotonic()
                if meeting_id in _seen and now_ts - _seen[meeting_id] < _DEDUP_TTL:
                    logger.info("Duplicate meeting.end for %s within %ds, skipping", meeting_id, _DEDUP_TTL)
                    continue
                _seen[meeting_id] = now_ts

                logger.info("Meeting ended: %s", meeting_id)
                asyncio.create_task(_guarded(on_meeting_end, event))

        except Exception as e:
            logger.error("Event listener error: %s", e)
        finally:
            proc.kill()
            logger.warning("Event stream disconnected, reconnecting in 5s...")
            await asyncio.sleep(5)

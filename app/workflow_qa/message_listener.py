import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

from app.config import settings
from app.lark import stream_lark_events

logger = logging.getLogger(__name__)

OnMessageCallback = Callable[[str, str], Awaitable[None]]

_DEDUP_TTL = 600  # 10 minutes — suppress re-delivered messages
_seen_messages: dict[str, float] = {}  # message_id → monotonic timestamp
_sem = asyncio.Semaphore(2)  # at most 2 concurrent Q&A responses


async def _guarded(on_message: OnMessageCallback, open_id: str, text: str) -> None:
    async with _sem:
        await on_message(open_id, text)


async def listen_for_dm_messages(on_message: OnMessageCallback) -> None:
    """
    Consume lark-cli event +subscribe NDJSON stream indefinitely.
    Calls on_message(sender_open_id, text) when a p2p text message arrives.
    - Only handles im.message.receive_v1 events.
    - Only handles p2p (DM) chat type.
    - Only handles text message type.
    - Deduplicates by message_id within 10 minutes.
    - Auto-reconnects on process failure.
    """
    while True:
        logger.info("Starting lark IM message subscription...")
        proc = await stream_lark_events(
            "--filter",
            settings.IM_MESSAGE_FILTER,
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

                # Resolve event_type (compact format may differ)
                event_type = event.get("event_type", "") or event.get("header", {}).get(
                    "event_type", ""
                )
                if "im.message.receive" not in event_type:
                    continue

                msg = event.get("event", {}).get("message", {})
                sender = event.get("event", {}).get("sender", {})

                # Only handle p2p (DM) text messages
                if msg.get("chat_type") != "p2p":
                    continue
                if msg.get("message_type") != "text":
                    continue

                message_id = msg.get("message_id", "")
                if not message_id:
                    continue

                # Dedup
                now_ts = time.monotonic()
                if message_id in _seen_messages and now_ts - _seen_messages[message_id] < _DEDUP_TTL:
                    logger.debug("Duplicate message %s, skipping", message_id)
                    continue
                _seen_messages[message_id] = now_ts

                open_id = sender.get("sender_id", {}).get("open_id", "")
                if not open_id:
                    continue

                # Parse text content (Feishu wraps it as JSON string: {"text": "..."})
                try:
                    content_raw = msg.get("content", "{}")
                    text = json.loads(content_raw).get("text", "").strip()
                except (json.JSONDecodeError, AttributeError):
                    text = ""
                if not text:
                    continue

                logger.info("DM from %s: %s", open_id, text[:80])
                asyncio.create_task(_guarded(on_message, open_id, text))

        except Exception as e:
            logger.error("Message listener error: %s", e)
        finally:
            proc.kill()
            logger.warning("IM message stream disconnected, reconnecting in 5s...")
            await asyncio.sleep(5)

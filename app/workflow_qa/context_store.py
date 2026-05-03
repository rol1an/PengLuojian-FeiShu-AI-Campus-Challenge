import time
import logging
from dataclasses import dataclass

from app.models import CalendarEvent, MeetingBrief

logger = logging.getLogger(__name__)


@dataclass
class ContextEntry:
    event: CalendarEvent
    brief: MeetingBrief
    inserted_at: float  # time.monotonic()


class MeetingContextStore:
    _TTL_SECONDS: int = 4 * 3600  # 4 hours

    def __init__(self) -> None:
        self._store: dict[str, ContextEntry] = {}

    def put(self, open_id: str, event: CalendarEvent, brief: MeetingBrief) -> None:
        self._evict_expired()
        self._store[open_id] = ContextEntry(
            event=event,
            brief=brief,
            inserted_at=time.monotonic(),
        )

    def get(self, open_id: str) -> ContextEntry | None:
        entry = self._store.get(open_id)
        if entry is None:
            return None
        if time.monotonic() - entry.inserted_at > self._TTL_SECONDS:
            del self._store[open_id]
            return None
        return entry

    def size(self) -> int:
        return len(self._store)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            uid for uid, entry in self._store.items()
            if now - entry.inserted_at > self._TTL_SECONDS
        ]
        for uid in expired:
            del self._store[uid]
        if expired:
            logger.debug("Evicted %d expired context entries", len(expired))


context_store = MeetingContextStore()

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from app.models import CalendarEvent, MeetingBrief, WikiDoc, ContextBullet
import app.persistence as db

logger = logging.getLogger(__name__)

_MAX_HISTORY = 10  # max turns kept in memory per user


@dataclass
class QATurn:
    question: str
    answer: str
    ts: float  # time.time()


@dataclass
class ContextEntry:
    event: CalendarEvent
    brief: MeetingBrief
    inserted_at: float  # time.monotonic()
    history: list[QATurn] = field(default_factory=list)


# ── Serialization helpers ─────────────────────────────────────────────────────

def _dt_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _serialize_event(event: CalendarEvent) -> str:
    return json.dumps(asdict(event), default=_dt_default)


def _deserialize_event(s: str) -> CalendarEvent:
    d = json.loads(s)
    return CalendarEvent(
        event_id=d["event_id"],
        title=d["title"],
        description=d["description"],
        start_time=datetime.fromisoformat(d["start_time"]),
        end_time=datetime.fromisoformat(d["end_time"]),
        attendee_open_ids=d["attendee_open_ids"],
        organizer_open_id=d["organizer_open_id"],
        attendee_names=d.get("attendee_names", {}),
        bound_chat_id=d.get("bound_chat_id", ""),
    )


def _serialize_brief(brief: MeetingBrief) -> str:
    return json.dumps(asdict(brief), default=_dt_default)


def _deserialize_brief(s: str) -> MeetingBrief:
    d = json.loads(s)
    return MeetingBrief(
        one_line=d["one_line"],
        context_bullets=[ContextBullet(**b) for b in d["context_bullets"]],
        key_docs=[WikiDoc(**doc) for doc in d["key_docs"]],
        open_questions=d["open_questions"],
    )


def _serialize_history(history: list[QATurn]) -> str:
    return json.dumps([asdict(t) for t in history])


def _deserialize_history(s: str) -> list[QATurn]:
    return [QATurn(**t) for t in json.loads(s)]


# ── Store ─────────────────────────────────────────────────────────────────────

class MeetingContextStore:
    _TTL_SECONDS: int = 4 * 3600  # 4 hours

    def __init__(self) -> None:
        self._store: dict[str, ContextEntry] = {}
        self._created_at: dict[str, float] = {}  # open_id → wall-clock insertion time
        db.init_db()
        self._load_from_db()

    def _load_from_db(self) -> None:
        now_wall = time.time()
        now_mono = time.monotonic()
        rows = db.qa_context_load_all()
        to_delete: list[str] = []
        loaded = 0
        for open_id, event_json, brief_json, history_json, created_at in rows:
            elapsed = now_wall - created_at
            if elapsed > self._TTL_SECONDS:
                to_delete.append(open_id)
                continue
            try:
                self._store[open_id] = ContextEntry(
                    event=_deserialize_event(event_json),
                    brief=_deserialize_brief(brief_json),
                    inserted_at=now_mono - elapsed,
                    history=_deserialize_history(history_json),
                )
                self._created_at[open_id] = created_at
                loaded += 1
            except Exception as e:
                logger.warning("Failed to load qa_context for %s: %s", open_id, e)
                to_delete.append(open_id)
        db.qa_context_delete(to_delete)
        if loaded:
            logger.info("Loaded %d qa_context entries from DB", loaded)

    def put(self, open_id: str, event: CalendarEvent, brief: MeetingBrief) -> None:
        self._evict_expired()
        now_wall = time.time()
        self._store[open_id] = ContextEntry(
            event=event,
            brief=brief,
            inserted_at=time.monotonic(),
        )
        self._created_at[open_id] = now_wall
        db.qa_context_put(open_id, _serialize_event(event), _serialize_brief(brief), now_wall)

    def append_turn(self, open_id: str, question: str, answer: str) -> None:
        """Append a Q&A turn to the conversation history for open_id."""
        entry = self._store.get(open_id)
        if entry is None:
            return
        entry.history.append(QATurn(question=question, answer=answer, ts=time.time()))
        if len(entry.history) > _MAX_HISTORY:
            entry.history = entry.history[-_MAX_HISTORY:]
        db.qa_context_update_history(open_id, _serialize_history(entry.history))

    def get(self, open_id: str) -> ContextEntry | None:
        entry = self._store.get(open_id)
        if entry is None:
            return None
        if time.monotonic() - entry.inserted_at > self._TTL_SECONDS:
            del self._store[open_id]
            self._created_at.pop(open_id, None)
            db.qa_context_delete([open_id])
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
            self._created_at.pop(uid, None)
        db.qa_context_delete(expired)
        if expired:
            logger.debug("Evicted %d expired context entries", len(expired))


context_store = MeetingContextStore()

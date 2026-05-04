from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ChatMessage:
    sender_open_id: str
    sender_name: str
    content: str          # plain text
    timestamp: datetime
    chat_id: str
    source: str           # "dm" | "group"
    chat_name: str = ""   # DM: the other party's display name; group: group name


@dataclass
class ContextBullet:
    text: str
    source_label: str  # e.g. "04-28 14:30 · 张三（私聊）"


@dataclass
class MeetingBrief:
    one_line: str
    context_bullets: list[ContextBullet]  # key points from chats, 2-4 items
    key_docs: "list[WikiDoc]"             # top 3 docs
    open_questions: list[str]             # 1-3 unresolved points


@dataclass
class CalendarEvent:
    event_id: str
    title: str
    description: str
    start_time: datetime
    end_time: datetime
    attendee_open_ids: list[str]
    organizer_open_id: str
    attendee_names: dict[str, str] = field(default_factory=dict)  # open_id → display_name
    bound_chat_id: str = ""  # group chat directly bound to this event (from API field or description)


@dataclass
class WikiDoc:
    title: str
    url: str
    space_name: str
    node_token: str
    obj_token: str = ""
    excerpt: str = ""
    score: float = 0.0
    why_relevant: str = ""
    key_conclusions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    anchor_url: str = ""
    obj_edit_time: str = ""   # unix timestamp string, for recency comparison
    task_background: str = ""
    how_to_solve: str = ""
    related_parties: str = ""
    link_reason: str = ""


@dataclass
class ActionItem:
    summary: str
    assignee_name: str
    assignee_open_id: str | None
    due_hint: str
    start_hint: str
    context: str
    wiki_links: list[WikiDoc] = field(default_factory=list)


@dataclass
class MeetingRecord:
    meeting_id: str
    calendar_event_id: str | None
    minute_token: str | None
    title: str
    end_time: datetime
    participant_open_ids: list[str]
    transcript: str = ""
    ai_summary: str = ""
    name_to_open_id: dict[str, str] = field(default_factory=dict)

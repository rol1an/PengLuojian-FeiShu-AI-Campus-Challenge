from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CalendarEvent:
    event_id: str
    title: str
    description: str
    start_time: datetime
    end_time: datetime
    attendee_open_ids: list[str]
    organizer_open_id: str


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

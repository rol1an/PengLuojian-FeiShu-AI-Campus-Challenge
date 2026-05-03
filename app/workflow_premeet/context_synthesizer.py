"""Synthesize pre-meeting brief from 4 information sources via LLM."""
import json
import logging
import re
from datetime import timezone, timedelta

from app.llm import call_llm
from app.models import CalendarEvent, ChatMessage, ContextBullet, MeetingBrief, WikiDoc

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))
_PLACEHOLDER_RE = re.compile(r"^用户\d+$")


def _best_name(sender_name: str, chat_name: str = "") -> str:
    """Return the best non-placeholder display name.

    Feishu assigns anonymous display names like '用户604098', '用户12345', etc.
    to external/restricted users. When sender_name matches this pattern,
    fall back to chat_name (DM chat titles typically equal the other party's
    real display name from the calendar attendee list).
    """
    if sender_name and not _PLACEHOLDER_RE.match(sender_name):
        return sender_name
    if chat_name and not _PLACEHOLDER_RE.match(chat_name):
        return chat_name
    return sender_name  # return whatever we have, even if still a placeholder

SELECT_SYSTEM = """You are a meeting preparation assistant.
Given recent chat messages before a meeting, select the most relevant sentences.

Return ONLY valid JSON (no markdown fences):
{
  "selected": [
    {"msg_idx": 0, "text": "原句片段", "reason": "为什么重要"},
    ...
  ]
}

Rules:
- Select 3-5 sentences most likely to come up in the meeting
- Prioritize: disputes, progress updates, agreements, action items
- "msg_idx" must be the integer index from the message list (the number in brackets at the start of each line)
- "text" must be a verbatim fragment from the messages (do not paraphrase)
- "reason" is one short phrase in Chinese: e.g. "待确认约定", "当前进展", "分歧点", "待办"
- If no chat messages provided, return {"selected": []}
- Language: Chinese
"""

SYNTHESIZE_SYSTEM = """You are a meeting preparation assistant.
Given information about an upcoming meeting, synthesize a concise pre-meeting brief.

Return ONLY valid JSON (no markdown fences):
{
  "one_line": "one sentence about what this meeting is mainly solving/deciding",
  "context_bullets": [
    {"text": "bullet 1", "source_idx": 0},
    {"text": "bullet 2", "source_idx": 1},
    {"text": "bullet 3", "source_idx": 2}
  ],
  "open_questions": ["unresolved point 1", "unresolved point 2"]
}

Rules:
- one_line: max 30 words, focus on the core purpose/decision, not just restate the title
- context_bullets: exactly 3 items, each a natural complete sentence (~40-50 Chinese characters).
  Base them ONLY on the provided key sentences from recent chats.
  Each bullet should include: (1) who is involved — ONLY use the exact name of a real person that explicitly appears in the key sentences. NEVER invent or generalize with role labels like "产品侧"/"团队"/"用户"/"相关人员"/"负责人"/"大家"/"各方". If no real person's name is present, start the bullet with the topic directly (omit the subject entirely), (2) what is being discussed, (3) current status — where it's stuck or how far along.
  Write like a human briefing a colleague, not a formal summary.
  "source_idx" must be the integer index (0-based) of the key sentence this bullet is most derived from.
  If no key sentences are provided, return [].
- open_questions: 1-3 specific unresolved points that should be addressed in the meeting, max 20 words each. If nothing clear, return [].
- Language: match the meeting title's language (Chinese title → all Chinese text values)
"""


async def synthesize_brief(
    event: CalendarEvent,
    dm_messages: list[ChatMessage],
    group_messages: list[ChatMessage],
    docs: list[WikiDoc],
    im_focus: str = "",
) -> MeetingBrief:
    """Synthesize a MeetingBrief from all available context. Degrades gracefully on failure."""
    key_docs = docs[:3]

    try:
        # Step 1: select key sentences from chat messages
        selected, is_llm_selected = await _select_key_sentences(
            dm_messages, group_messages, event.title, im_focus=im_focus
        )
        logger.info("Selected %d key sentences for '%s' (llm=%s)", len(selected), event.title, is_llm_selected)

        # Step 2: synthesize one_line and open_questions via LLM
        user_msg = _build_user_message(event, selected if is_llm_selected else [], key_docs)
        raw = await call_llm(SYNTHESIZE_SYSTEM, user_msg)
        data = _parse_json_object(raw)

        one_line = data.get("one_line", "") or event.title
        open_questions = data.get("open_questions", [])

        # Step 3: build context_bullets
        # If LLM selected relevant sentences: let LLM synthesize bullets (existing logic)
        # If fallback (non-relevant msgs): build bullets directly from raw messages, no LLM rewrite
        context_bullets: list[ContextBullet] = []

        if is_llm_selected and selected:
            for bullet_data in data.get("context_bullets", []):
                if isinstance(bullet_data, dict):
                    text = bullet_data.get("text", "")
                    source_idx = bullet_data.get("source_idx")
                else:
                    text = str(bullet_data)
                    source_idx = None

                source_label = ""
                if source_idx is not None and 0 <= int(source_idx) < len(selected):
                    sel = selected[int(source_idx)]
                    t = sel.get("time", "")
                    is_dm = sel.get("source") == "dm"
                    if is_dm:
                        name = _best_name(sel.get("chat_name", ""), sel.get("sender_name", ""))
                        source_label = f"{t}与{name}（私聊）" if name else f"{t}（私聊）"
                    else:
                        name = _best_name(sel.get("sender_name", ""), sel.get("chat_name", ""))
                        source_label = f"{t}来自{name}（群聊）" if name else f"{t}（群聊）"
                if text:
                    context_bullets.append(ContextBullet(text=text, source_label=source_label))
        elif selected:
            # Fallback: use raw message content verbatim, no LLM rewrite to avoid hallucination
            for sel in selected:
                t = sel.get("time", "")
                is_dm = sel.get("source") == "dm"
                text = sel.get("text", "").strip()
                if not text:
                    continue
                if is_dm:
                    name = _best_name(sel.get("chat_name", ""), sel.get("sender_name", ""))
                    source_label = f"{t}与{name}（私聊）" if name else f"{t}（私聊）"
                else:
                    name = _best_name(sel.get("sender_name", ""), sel.get("chat_name", ""))
                    source_label = f"{t}来自{name}（群聊）" if name else f"{t}（群聊）"
                context_bullets.append(ContextBullet(text=text, source_label=source_label))

        return MeetingBrief(
            one_line=one_line,
            context_bullets=context_bullets,
            key_docs=key_docs,
            open_questions=open_questions,
        )

    except Exception as e:
        logger.warning("synthesize_brief failed, using fallback: %s", e)
        return _fallback_brief(event, key_docs)


async def _select_key_sentences(
    dm_messages: list[ChatMessage],
    group_messages: list[ChatMessage],
    meeting_title: str,
    im_focus: str = "",
) -> tuple[list[dict], bool]:
    """Step 1: Ask LLM to pick the most meeting-relevant sentences from raw chat messages.

    Returns (selected_items, is_llm_selected).
    is_llm_selected=True means LLM found relevant sentences (safe for synthesis).
    is_llm_selected=False means fallback to recent raw messages (use verbatim, no LLM rewrite).
    """
    # Interleave DM and group messages by timestamp so SELECT sees the most recent
    # messages across both sources, rather than all DMs always appearing first.
    all_msgs = sorted(dm_messages + group_messages, key=lambda m: m.timestamp, reverse=True)[:20]
    if not all_msgs:
        return [], False
    lines = [
        f"[{i}] [{m.timestamp.astimezone(_CST).strftime('%m-%d %H:%M')} {m.sender_name}] {m.content[:200]}"
        for i, m in enumerate(all_msgs)
    ]
    user_msg = f"Meeting: {meeting_title}\n\nMessages:\n" + "\n".join(lines)
    if im_focus:
        user_msg += f"\n\n== 本次会议的信息需求 ==\n{im_focus}\n在选句时，优先选择能回答上述信息需求的消息。"
    try:
        raw = await call_llm(SELECT_SYSTEM, user_msg)
        data = _parse_json_object(raw)
        selected = data.get("selected", [])
        # Enrich each selected item with source metadata via msg_idx
        for item in selected:
            msg_idx = item.get("msg_idx")
            if msg_idx is not None and 0 <= int(msg_idx) < len(all_msgs):
                msg = all_msgs[int(msg_idx)]
                item["time"] = msg.timestamp.astimezone(_CST).strftime("%m月%d日 %H:%M")
                item["sender_name"] = msg.sender_name
                item["source"] = msg.source
                item["chat_name"] = msg.chat_name
        if selected:
            return selected, True
        # Fallback: LLM found nothing relevant — use 3 most recent messages verbatim
        logger.info("LLM selected 0 sentences, falling back to 3 most recent messages (verbatim)")
        fallback_msgs = all_msgs[-3:]
        fallback = []
        for msg in fallback_msgs:
            fallback.append({
                "msg_idx": all_msgs.index(msg),
                "text": msg.content[:100],
                "reason": "近期消息",
                "time": msg.timestamp.astimezone(_CST).strftime("%m月%d日 %H:%M"),
                "sender_name": msg.sender_name,
                "source": msg.source,
                "chat_name": msg.chat_name,
            })
        return fallback, False
    except Exception as e:
        logger.debug("_select_key_sentences failed: %s", e)
        return [], False


def _build_user_message(
    event: CalendarEvent,
    selected: list[dict],
    key_docs: list[WikiDoc],
) -> str:
    start_str = event.start_time.astimezone(_CST).strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Meeting",
        f"Title: {event.title}",
        f"Time: {start_str}",
    ]
    if event.description:
        lines.append(f"Agenda: {event.description[:300]}")

    if selected:
        lines.append("\n# Key sentences from recent chats")
        for s in selected:
            name = _best_name(s.get("sender_name", ""), s.get("chat_name", ""))
            prefix = f"[{name}说] " if name else ""
            lines.append(f'- {prefix}"{s["text"]}" ({s.get("reason", "")})')

    if key_docs:
        lines.append("\n# Related documents")
        for doc in key_docs:
            lines.append(f"- {doc.title}")
            if doc.why_relevant:
                lines.append(f"  Relevance: {doc.why_relevant}")
            if doc.key_conclusions:
                lines.append(f"  Conclusions: {'; '.join(doc.key_conclusions)}")
            if doc.open_questions:
                lines.append(f"  Open questions: {'; '.join(doc.open_questions)}")

    return "\n".join(lines)


def _fallback_brief(event: CalendarEvent, key_docs: list[WikiDoc]) -> MeetingBrief:
    open_questions: list[str] = []
    for doc in key_docs:
        open_questions.extend(doc.open_questions)
    return MeetingBrief(
        one_line=event.title,
        context_bullets=[],  # list[ContextBullet]
        key_docs=key_docs,
        open_questions=open_questions[:2],
    )


def _parse_json_object(raw: str) -> dict:
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except (json.JSONDecodeError, AttributeError):
        pass
    return {}

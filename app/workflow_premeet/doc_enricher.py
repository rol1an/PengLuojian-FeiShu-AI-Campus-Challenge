"""Layer 3: Async document enrichment.

After the basic card is pushed, this module fetches the top N document bodies,
extracts why_relevant / key_conclusions / open_questions via LLM, then re-pushes
an enriched card to all attendees. Failures degrade silently.
"""
import asyncio
import json
import logging
import re

from app.lark import run_lark
from app.llm import call_llm
from app.models import CalendarEvent, WikiDoc
from app.workflow_premeet.card_builder import build_enriched_knowledge_card
from app.workflow_premeet.push_service import push_card_payload

logger = logging.getLogger(__name__)

_ENRICH_TOP_N = 3
_MAX_CONTENT_CHARS = 20000

ENRICH_SYSTEM = """You are a meeting preparation assistant.
Given a meeting title and a wiki document's content, extract the following in JSON:
{
  "why_relevant": "one sentence explaining why this doc matters for the meeting",
  "key_conclusions": ["conclusion 1", "conclusion 2"],
  "open_questions": ["question 1", "question 2"],
  "anchor_url": "the most relevant section anchor URL, or null",
  "excerpts": [
    {
      "section": "章节标题或来源位置（如'第二节：架构设计'）",
      "content": "该段原文（最多300字，不要改写，如有列举项须完整保留所有条目）"
    }
  ]
}

Rules:
- why_relevant: max 20 words, specific to the meeting
- key_conclusions: exactly 2 items. Each must be a complete standalone sentence (~25 Chinese characters) with 3 elements: (1) Subject/Topic: who or what this is about, (2) Result/State: what was achieved/found/decided, (3) Significance: why it matters for this meeting. Example: "RAG召回模块已完成改造，准确率提升至92%，是本次架构讨论的核心基线。"
- open_questions: 1-2 items that this meeting should address, max 15 words each
- anchor_url: null unless you see a direct anchor link in the content
- excerpts: select 1-2 paragraphs from the document most relevant to the meeting topic. Preserve the original text verbatim (do not paraphrase). Mark the section title or location. If the paragraph contains enumerated items (①②③ or numbered lists), include ALL items completely — do not truncate mid-list.
- **Language: respond in the same language as the meeting title. If the meeting title is Chinese, all text values must be in Chinese.**
- Return ONLY valid JSON, no markdown fences
"""


async def enrich_docs_inplace(docs: list[WikiDoc], meeting_title: str) -> None:
    """Enrich top docs in-place (fill why_relevant / conclusions / questions). No pushing."""
    if not docs:
        return
    tasks = [_enrich_one(doc, meeting_title) for doc in docs]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for doc, result in zip(docs, results):
        if isinstance(result, Exception):
            logger.debug("Enrichment skipped for '%s': %s", doc.title, result)


async def enrich_and_repush(
    docs: list[WikiDoc],
    event: CalendarEvent,
) -> None:
    """
    Async entry point called via asyncio.create_task().
    Enriches top N docs then re-pushes card to all attendees.
    """
    if not docs or not event.attendee_open_ids:
        return

    top_docs = docs[: _ENRICH_TOP_N]
    enriched_any = False

    tasks = [_enrich_one(doc, event.title) for doc in top_docs]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for doc, result in zip(top_docs, results):
        if isinstance(result, Exception):
            logger.debug("Enrichment skipped for '%s': %s", doc.title, result)
        else:
            enriched_any = True

    from app.workflow_premeet.push_service import push_knowledge_to_participants

    if not enriched_any:
        logger.info("No docs enriched; falling back to basic card for %s", event.event_id)
        await push_knowledge_to_participants(event, docs)
        return

    payload = build_enriched_knowledge_card(event, docs)
    await push_card_payload(payload, event.attendee_open_ids, event.event_id, label="enriched")


async def _enrich_one(doc: WikiDoc, meeting_title: str) -> None:
    """Fetch document body and extract enrichment fields in-place."""
    if not doc.obj_token:
        raise ValueError("no obj_token")

    content = await _fetch_doc_content(doc.obj_token)
    if not content:
        raise ValueError("empty content")

    snippet = content[:_MAX_CONTENT_CHARS]
    user_msg = f"Meeting title: {meeting_title}\n\nDocument title: {doc.title}\n\nContent:\n{snippet}"
    raw = await call_llm(ENRICH_SYSTEM, user_msg)

    data = _parse_json_object(raw)
    doc.why_relevant = data.get("why_relevant", "")
    doc.key_conclusions = data.get("key_conclusions", [])
    doc.open_questions = data.get("open_questions", [])
    anchor = data.get("anchor_url")
    if anchor and isinstance(anchor, str):
        doc.anchor_url = anchor
    excerpts = data.get("excerpts", [])
    if excerpts:
        doc.excerpt = "\n".join(
            f"【{e.get('section', '节选')}】{e.get('content', '')}"
            for e in excerpts[:2]
            if isinstance(e, dict)
        )


async def _fetch_doc_content(obj_token: str) -> str:
    """Fetch markdown content of a doc via lark-cli docs +fetch."""
    try:
        data = await run_lark(
            "docs", "+fetch",
            "--doc", obj_token,
            as_identity="user",
            timeout=20,
        )
        return data.get("data", {}).get("markdown", "")
    except Exception as e:
        logger.debug("Failed to fetch doc content for %s: %s", obj_token, e)
        return ""


def _parse_json_object(raw: str) -> dict:
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except (json.JSONDecodeError, AttributeError):
        pass
    return {}

import json
import logging
import re

from app.llm import call_llm
from app.models import ActionItem

logger = logging.getLogger(__name__)

ACTION_EXTRACTION_SYSTEM = """You are a meeting minutes analyst.
Extract all action items from the provided meeting transcript or summary.

Return ONLY a valid JSON array. Each element must have exactly these fields:
{
  "summary": "One clear sentence describing the action (starts with a verb)",
  "assignee_name": "Full name of the person responsible exactly as mentioned, or empty string",
  "due_hint": "Due date or timeframe as mentioned (e.g. 'by Friday', 'next week'), or empty string",
  "context": "One sentence of relevant background from the meeting"
}

Rules:
- Only extract real commitments and decisions, not suggestions
- If no action items exist, return []
- Do not invent information not present in the transcript
- assignee_name must be the name exactly as spoken/written
- summary must start with a verb and be concise and actionable
"""


async def extract_action_items(transcript: str, ai_summary: str = "") -> list[ActionItem]:
    """Use Claude to extract structured action items from meeting content."""
    content = transcript if len(transcript) >= len(ai_summary) else ai_summary
    if not content.strip():
        logger.warning("Empty meeting content, skipping action extraction")
        return []

    # Truncate to avoid token limits; action items are usually near the end
    if len(content) > 8000:
        content = "[...transcript truncated...]\n" + content[-8000:]

    raw = await call_llm(ACTION_EXTRACTION_SYSTEM, f"Meeting content:\n\n{content}")

    try:
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if not match:
            logger.warning("No JSON array in LLM response: %s", raw[:200])
            return []
        items_data = json.loads(match.group(0))
        return [
            ActionItem(
                summary=item["summary"],
                assignee_name=item.get("assignee_name", ""),
                assignee_open_id=None,
                due_hint=item.get("due_hint", ""),
                context=item.get("context", ""),
            )
            for item in items_data
            if item.get("summary")
        ]
    except (json.JSONDecodeError, KeyError) as e:
        logger.error("Failed to parse action items: %s\nRaw: %s", e, raw[:300])
        return []

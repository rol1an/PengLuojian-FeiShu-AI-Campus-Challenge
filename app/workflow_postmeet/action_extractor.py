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
  "assignee_name": "Name of the person responsible (see rules below), or empty string if truly unresolvable",
  "due_hint": "Deadline as mentioned: 'ddl在...', '...之前完成', '最晚...', 'by Friday', etc. Empty string if not mentioned.",
  "start_hint": "Start time as mentioned: '明天开始', '下周一再...', '这件事后天...', etc. Empty string if not mentioned.",
  "context": "One sentence of relevant background from the meeting"
}

## Assignee resolution rules (apply in order):

1. **Explicit name**: "让小明去做" / "assigned to Alice" → that person
2. **First-person commitment**: speaker says "我会" / "我来" / "我去" / "I will" → the speaker
3. **Second-person directive**: speaker says "你去" / "你来" / "你要" / "you should" → the person being spoken to (infer from conversation context — who is the other active participant in that exchange?)
4. **Third-person reference**: "他下一步" / "她负责" → named person mentioned nearby in the transcript
5. **First-person plural "我们"**: assign to the speaker (the person who said it is most accountable)
6. **No clear owner but clear internal team task**: assign to the speaker of that turn
7. **Clearly external / third-party task**: "供应商的想法是..." / "客户需要..." → leave assignee_name empty
8. **Purely organizational goal without owner**: "公司目标是..." / "下一步方向是..." → leave assignee_name empty

## Other rules:
- Only extract real commitments and decisions, not vague suggestions or observations
- If no action items exist, return []
- summary must start with a verb, be concise and actionable, written in the same language as the transcript
- due_hint: only extract explicit deadline signals; do NOT infer from vague hints like "下周" alone
- start_hint: only extract explicit start signals; "下周做" is NOT a start hint, "明天开始做" IS
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
                start_hint=item.get("start_hint", ""),
                context=item.get("context", ""),
            )
            for item in items_data
            if item.get("summary")
        ]
    except (json.JSONDecodeError, KeyError) as e:
        logger.error("Failed to parse action items: %s\nRaw: %s", e, raw[:300])
        return []

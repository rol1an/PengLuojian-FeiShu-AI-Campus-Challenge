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
    items_data = _parse_json_array(raw)

    if items_data is None:
        logger.warning("JSON parse failed, retrying with stricter prompt")
        raw = await call_llm(
            ACTION_EXTRACTION_SYSTEM,
            f"Meeting content:\n\n{content}\n\nReturn ONLY a raw JSON array, no markdown.",
        )
        items_data = _parse_json_array(raw)

    if items_data is None:
        logger.error("Failed to parse action items after retry. Raw: %s", raw[:300])
        return []

    result = []
    for item in items_data:
        if not item.get("summary"):
            continue
        result.append(ActionItem(
            summary=item["summary"],
            assignee_name=item.get("assignee_name", ""),
            assignee_open_id=None,
            due_hint=item.get("due_hint", ""),
            start_hint=item.get("start_hint", ""),
            context=item.get("context", ""),
        ))
    return result


def _parse_json_array(raw: str) -> list | None:
    """Extract a JSON array from LLM output. Returns None if parsing fails."""
    # 1. Try code fence
    fence_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # 2. Slice from first '[' to last ']'
    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass

    return None

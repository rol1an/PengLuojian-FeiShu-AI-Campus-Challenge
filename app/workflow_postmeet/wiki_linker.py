"""
Post-meeting wiki linker.

Pipeline per action item:
  1. Keyword extraction (LLM) — summary + context → search terms + system names
  2. Multi-path candidate retrieval (parallel wiki searches, deduped)
  3. Rule-based quality pre-filter
  4. LLM precision selection with meeting context — outputs node_tokens + link_reason
"""
import asyncio
import json
import logging
import re
from datetime import datetime

from app.config import settings
from app.lark import run_lark
from app.llm import call_llm
from app.models import ActionItem, WikiDoc

logger = logging.getLogger(__name__)

# ── Prompts ──────────────────────────────────────────────────────────────────

KEYWORD_EXTRACT_SYSTEM = """You are a search query optimizer.
Given a meeting action item (summary + context), extract search terms in JSON:
{
  "terms": ["term1", "term2", "term3"],
  "system_names": ["SystemA", "ModuleB"]
}

Rules:
- terms: 3-6 short noun phrases or keywords (NOT the full sentence). Extract nouns, system names, technical terms, module names.
- system_names: product/module/API names explicitly mentioned (e.g. "lark client", "飞书日历", "wiki API"). Empty list if none.
- Output in the same language as the input.
- Return ONLY valid JSON, no markdown fences.
"""

RERANK_SYSTEM = """You are a knowledge base curator for a team meeting.
Given a meeting action item and candidate wiki documents, select the 1-3 documents most useful for completing the task.

Return ONLY a JSON array of objects. Each selected document must have:
{
  "node_token": "token from candidates",
  "link_reason": "一句话说明这篇文档能帮助完成此任务的具体原因，20字内，中文"
}

Rules:
- Only select documents that directly help execute the action item
- link_reason must be specific to this action item (not a generic summary)
- If no document is relevant enough, return []
- Return ONLY valid JSON array, no markdown fences
"""

_TEMPLATE_KEYWORDS = {"模板", "示例", "untitled", "test", "draft", "template", "example"}


def _item_to_doc(n: dict) -> WikiDoc:
    return WikiDoc(
        title=n.get("title", ""),
        url=f"https://your-domain.feishu.cn/wiki/{n.get('node_token', '')}",
        space_name=n.get("space_name", ""),
        node_token=n.get("node_token", ""),
        obj_token=n.get("obj_token", ""),
        excerpt=n.get("excerpt", ""),
    )


async def enrich_with_wiki_links(
    items: list[ActionItem],
    meeting_title: str = "",
    meeting_summary: str = "",
) -> list[ActionItem]:
    """For each action item, find and link relevant wiki docs."""
    async def _safe(item: ActionItem) -> None:
        try:
            item.wiki_links = await _find_links_for_item(item, meeting_title, meeting_summary)
        except Exception as e:
            logger.warning("Wiki linking failed for '%s': %s", item.summary[:50], e)

    await asyncio.gather(*[_safe(item) for item in items])
    return items


async def _find_links_for_item(
    item: ActionItem,
    meeting_title: str,
    meeting_summary: str,
) -> list[WikiDoc]:
    # Step 1: Extract search keywords via LLM
    kw_raw = await call_llm(
        KEYWORD_EXTRACT_SYSTEM,
        f"Action item: {item.summary}\nContext: {item.context}",
    )
    terms, system_names = _parse_keywords(kw_raw, item.summary)
    logger.debug("Keywords for '%s': terms=%s systems=%s", item.summary[:40], terms, system_names)

    # Step 2: Multi-path parallel search
    queries: list[str] = []
    queries.extend(terms[:3])          # top keyword terms
    queries.extend(system_names[:2])   # system/module names
    if meeting_title:
        queries.append(meeting_title)  # meeting title path

    # Deduplicate queries
    seen_q: set[str] = set()
    unique_queries = [q for q in queries if q and not (q in seen_q or seen_q.add(q))]  # type: ignore[func-returns-value]

    search_results = await asyncio.gather(
        *[_search_wiki(q) for q in unique_queries],
        return_exceptions=True,
    )

    # Merge and deduplicate candidates by node_token
    seen_tokens: set[str] = set()
    candidates: list[WikiDoc] = []
    for result in search_results:
        if isinstance(result, list):
            for doc in result:
                if doc.node_token not in seen_tokens:
                    seen_tokens.add(doc.node_token)
                    candidates.append(doc)

    if not candidates:
        return []

    # Step 3: Rule-based quality pre-filter
    candidates = _quality_filter(candidates)
    if not candidates:
        return []

    # Step 4: LLM precision selection with meeting context
    candidate_list = "\n".join(
        f"- node_token={d.node_token}: 《{d.title}》 [{d.space_name}]"
        for d in candidates
    )
    user_msg = (
        f"会议标题：{meeting_title}\n"
        f"会议摘要：{meeting_summary[:300] if meeting_summary else '无'}\n\n"
        f"Action item: {item.summary}\n"
        f"Context: {item.context}\n\n"
        f"候选文档：\n{candidate_list}"
    )
    raw = await call_llm(RERANK_SYSTEM, user_msg)
    selected_data = _parse_json_array(raw)

    if selected_data:
        token_to_reason = {
            entry["node_token"]: entry.get("link_reason", "")
            for entry in selected_data
            if isinstance(entry, dict) and entry.get("node_token")
        }
        selected = [d for d in candidates if d.node_token in token_to_reason]
        for doc in selected:
            doc.link_reason = token_to_reason.get(doc.node_token, "")
        if selected:
            return selected

    # Fallback: top 2, no link_reason
    return candidates[:2]


async def _search_wiki(query: str) -> list[WikiDoc]:
    """Single wiki search, returns list of WikiDoc."""
    params: dict = {"query": query, "count": 6}
    if settings.WIKI_SPACE_ID:
        params["space_id"] = settings.WIKI_SPACE_ID
    try:
        data = await run_lark(
            "wiki", "nodes", "list",
            "--params", json.dumps(params),
            as_identity="user",
            timeout=settings.WIKI_SEARCH_TIMEOUT,
        )
        return [
            _item_to_doc(n)
            for n in data.get("data", {}).get("items", [])
            if n.get("node_token")
        ]
    except Exception as e:
        logger.debug("Wiki search failed for query '%s': %s", query, e)
        return []


def _quality_filter(docs: list[WikiDoc]) -> list[WikiDoc]:
    """Drop obviously low-quality docs by title heuristics."""
    result = []
    for doc in docs:
        title_lower = doc.title.lower()
        if any(kw in title_lower for kw in _TEMPLATE_KEYWORDS):
            continue
        result.append(doc)
    return result


def _parse_keywords(raw: str, fallback_summary: str) -> tuple[list[str], list[str]]:
    """Parse keyword extraction response. Falls back to splitting summary."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
            terms = [t for t in data.get("terms", []) if isinstance(t, str) and t.strip()]
            systems = [s for s in data.get("system_names", []) if isinstance(s, str) and s.strip()]
            if terms:
                return terms, systems
        except json.JSONDecodeError:
            pass
    # Fallback: split summary on common delimiters
    parts = re.split(r"[，,、\s]+", fallback_summary)
    return [p for p in parts if len(p) > 1][:4], []


def _parse_json_array(raw: str) -> list | None:
    """Extract JSON array from LLM output."""
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None

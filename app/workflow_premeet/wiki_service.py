import json
import logging
import re
import time

from app.config import settings
from app.lark import run_lark
from app.llm import call_llm
from app.models import WikiDoc

logger = logging.getLogger(__name__)

KEYWORD_SYSTEM = """You are a meeting preparation assistant.
Given a meeting title and optional agenda description, extract 3-5 concise search keywords
that would find the most relevant wiki/knowledge base documents for participants to read before the meeting.

Rules:
- Return ONLY a JSON array of strings: ["keyword1", "keyword2", ...]
- Each keyword should be 1-4 words, specific and concrete
- Prefer technical terms, product names, project names, acronyms over generic words
- No duplicates, no generic words like "meeting" or "discussion"
"""

RERANK_SYSTEM = """You are a meeting preparation assistant.
Given a meeting title, agenda, and a list of candidate wiki documents,
score each document's relevance and value for participants to read before the meeting.

Return ONLY a JSON array of objects for up to {max_docs} most relevant documents, ordered by score descending:
[{{"node_token": "xxx", "score": 7.5, "reason": "one sentence"}}]

Score scale (0-10):
- 0-2: Clearly irrelevant
- 3-5: Weakly relevant
- 6-8: Relevant, useful background reading
- 9-10: Highly relevant, directly actionable before this meeting

Only include documents with score >= 3. Omit clearly irrelevant ones.
Authority/role match is already factored in — focus on content relevance and decision value.
"""

_TEMPLATE_KEYWORDS = ("模板", "示例", "untitled", "test", "draft")


async def generate_keywords(title: str, description: str = "") -> list[str]:
    user_msg = f"Meeting title: {title}\nAgenda/Description: {description or '(none)'}"
    raw = await call_llm(KEYWORD_SYSTEM, user_msg)
    try:
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if not match:
            return [title]
        return json.loads(match.group(0))
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Keyword extraction failed, using title: %s", raw[:100])
        return [title]


async def search_wiki(
    keywords: list[str],
    title: str = "",
    description: str = "",
    attendee_open_ids: list[str] | None = None,
) -> list[WikiDoc]:
    """Search wiki, apply Layer 1 quality/relevance filtering, then Layer 2 LLM rerank."""
    if attendee_open_ids is None:
        attendee_open_ids = []

    seen: set[str] = set()
    raw_candidates: list[dict] = []
    max_candidates = settings.WIKI_MAX_DOCS * 2

    for kw in keywords:
        try:
            items = await _search_one_raw(kw)
            for item in items:
                node_token = item.get("node_token", "")
                if node_token and node_token not in seen:
                    seen.add(node_token)
                    raw_candidates.append(item)
                    if len(raw_candidates) >= max_candidates * 2:
                        break
        except Exception as e:
            logger.warning("Wiki search failed for '%s': %s", kw, e)
        if len(raw_candidates) >= max_candidates * 2:
            break

    if not raw_candidates:
        return []

    # Layer 1: compute quality + relevance scores, filter by quality threshold
    scored: list[tuple[dict, float, float]] = []
    for item in raw_candidates:
        q = _quality_score(item)
        r = _relevance_score(item, keywords, title, attendee_open_ids)
        if q >= settings.WIKI_QUALITY_THRESHOLD:
            scored.append((item, q, r))
        else:
            logger.debug(
                "Layer1 filtered out '%s' (quality=%.2f)", item.get("title", "?"), q
            )

    if not scored:
        return []

    # Sort by combined score: quality * 0.4 + relevance * 0.6
    scored.sort(key=lambda x: x[1] * 0.4 + x[2] * 0.6, reverse=True)

    # Take top max_candidates for LLM
    top = scored[:max_candidates]
    candidates = [_item_to_doc(item) for item, _q, _r in top]

    if len(candidates) <= settings.WIKI_MAX_DOCS:
        return candidates

    return await _rerank_with_llm(title, description, candidates, settings.WIKI_MAX_DOCS)


async def _rerank_with_llm(
    title: str,
    description: str,
    candidates: list[WikiDoc],
    max_docs: int,
) -> list[WikiDoc]:
    """Layer 2: LLM scores and reorders candidates; fills remainder if needed."""
    now_ts = time.time()

    def _age_label(doc: WikiDoc) -> str:
        # obj_edit_time stored in excerpt as fallback (not used here, age unknown)
        return ""

    candidate_list = "\n".join(
        f"- node_token={d.node_token}: {d.title} ({d.space_name})"
        for d in candidates
    )
    user_msg = (
        f"Meeting title: {title}\n"
        f"Agenda: {description or '(none)'}\n\n"
        f"Candidate documents:\n{candidate_list}"
    )
    raw = await call_llm(RERANK_SYSTEM.format(max_docs=max_docs), user_msg)

    try:
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if not match:
            return candidates[:max_docs]

        ranked = json.loads(match.group(0))
        # ranked: [{"node_token": "...", "score": 7.5, "reason": "..."}]
        token_to_score: dict[str, float] = {}
        token_order: dict[str, int] = {}
        for i, entry in enumerate(ranked):
            if isinstance(entry, dict) and "node_token" in entry:
                tok = entry["node_token"]
                token_to_score[tok] = float(entry.get("score", 0))
                token_order[tok] = i

        result: list[WikiDoc] = []
        for doc in candidates:
            if doc.node_token in token_order:
                doc.score = token_to_score[doc.node_token]
                result.append(doc)
        result.sort(key=lambda d: token_order[d.node_token])

        # Fill up to max_docs with remaining candidates if LLM returned fewer
        if len(result) < max_docs:
            selected = set(token_order.keys())
            extras = [d for d in candidates if d.node_token not in selected]
            result += extras[: max_docs - len(result)]

        return result[:max_docs]

    except (json.JSONDecodeError, AttributeError, ValueError):
        logger.warning("LLM rerank parse failed, using pre-sorted order")
        return candidates[:max_docs]


async def _search_one_raw(keyword: str) -> list[dict]:
    """Search wiki for one keyword, return raw API items."""
    params: dict = {"query": keyword, "count": 5}
    if settings.WIKI_SPACE_ID:
        params["space_id"] = settings.WIKI_SPACE_ID

    data = await run_lark(
        "wiki",
        "nodes",
        "list",
        "--params",
        json.dumps(params),
        as_identity="user",
        timeout=settings.WIKI_SEARCH_TIMEOUT,
    )
    return data.get("data", {}).get("items", [])


def _item_to_doc(item: dict) -> WikiDoc:
    node_token = item.get("node_token", "")
    return WikiDoc(
        title=item.get("title", "Untitled"),
        url=_build_wiki_url(node_token),
        space_name=item.get("space_name", ""),
        node_token=node_token,
        obj_token=item.get("obj_token", ""),
        excerpt=item.get("excerpt", ""),
    )


def _quality_score(item: dict) -> float:
    """Document intrinsic quality score (0~1), meeting-agnostic."""
    score = 0.0

    # Timeliness
    edit_time = item.get("obj_edit_time")
    if edit_time:
        try:
            age_days = (time.time() - int(edit_time)) / 86400
            if age_days < 30:
                score += 0.4
            elif age_days < 90:
                score += 0.2
            elif age_days > 180:
                score += 0.0
            else:
                score += 0.1
        except (ValueError, TypeError):
            pass

    # Has owner
    if item.get("owner"):
        score += 0.2

    # Title not a template/placeholder
    title_lower = item.get("title", "").lower()
    if any(kw in title_lower for kw in _TEMPLATE_KEYWORDS):
        score -= 0.3

    # Document type
    obj_type = item.get("obj_type", "")
    if obj_type in ("docx", "slides"):
        score += 0.2
    elif obj_type == "sheet":
        score += 0.1

    return score


def _relevance_score(
    item: dict,
    keywords: list[str],
    meeting_title: str,
    attendee_open_ids: list[str],
) -> float:
    """Rule-based meeting relevance score (0~1)."""
    score = 0.0

    # Keyword or meeting title words hit in doc title
    doc_title = item.get("title", "").lower()
    search_terms = [kw.lower() for kw in keywords] + meeting_title.lower().split()
    if any(term in doc_title for term in search_terms if len(term) > 1):
        score += 0.4

    # Attendee open_id matches owner or creator
    owner = item.get("owner", "")
    creator = item.get("creator", "")
    if any(uid in (owner, creator) for uid in attendee_open_ids):
        score += 0.3

    # Space name matches any keyword
    space_name = item.get("space_name", "").lower()
    if any(kw.lower() in space_name for kw in keywords if len(kw) > 1):
        score += 0.2

    return score


def _build_wiki_url(node_token: str) -> str:
    return f"https://your-domain.feishu.cn/wiki/{node_token}"

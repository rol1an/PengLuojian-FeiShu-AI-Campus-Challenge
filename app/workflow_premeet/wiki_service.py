import json
import logging
import re

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
select the most relevant documents for participants to read before the meeting.

Return ONLY a JSON array of node_token strings (up to {max_docs} items), ordered by relevance (most relevant first).
If none are relevant, return [].
Example: ["token_abc", "token_xyz"]
"""


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
) -> list[WikiDoc]:
    """Search wiki for each keyword, deduplicate, then rerank with LLM."""
    seen: set[str] = set()
    candidates: list[WikiDoc] = []
    max_candidates = settings.WIKI_MAX_DOCS * 2

    for kw in keywords:
        try:
            results = await _search_one(kw)
            for doc in results:
                if doc.node_token not in seen:
                    seen.add(doc.node_token)
                    candidates.append(doc)
                    if len(candidates) >= max_candidates:
                        break
        except Exception as e:
            logger.warning("Wiki search failed for '%s': %s", kw, e)
        if len(candidates) >= max_candidates:
            break

    if not candidates:
        return []
    if len(candidates) <= settings.WIKI_MAX_DOCS:
        return candidates

    return await _rerank_with_llm(title, description, candidates, settings.WIKI_MAX_DOCS)


async def _rerank_with_llm(
    title: str,
    description: str,
    candidates: list[WikiDoc],
    max_docs: int,
) -> list[WikiDoc]:
    """Use LLM to select and reorder the most relevant candidates for the meeting."""
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
        selected_tokens = json.loads(match.group(0))
        token_order = {t: i for i, t in enumerate(selected_tokens)}
        result = [d for d in candidates if d.node_token in token_order]
        result.sort(key=lambda d: token_order[d.node_token])
        # Fill up to max_docs with remaining candidates if LLM returned fewer
        if len(result) < max_docs:
            selected_tokens = set(token_order.keys())
            extras = [d for d in candidates if d.node_token not in selected_tokens]
            result += extras[: max_docs - len(result)]
        return result[:max_docs]
    except (json.JSONDecodeError, AttributeError):
        return candidates[:max_docs]


async def _search_one(keyword: str) -> list[WikiDoc]:
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

    results: list[WikiDoc] = []
    for item in data.get("data", {}).get("items", []):
        node_token = item.get("node_token", "")
        if not node_token:
            continue
        results.append(
            WikiDoc(
                title=item.get("title", "Untitled"),
                url=_build_wiki_url(node_token),
                space_name=item.get("space_name", ""),
                node_token=node_token,
                excerpt=item.get("excerpt", ""),
            )
        )
    return results


def _build_wiki_url(node_token: str) -> str:
    return f"https://your-domain.feishu.cn/wiki/{node_token}"

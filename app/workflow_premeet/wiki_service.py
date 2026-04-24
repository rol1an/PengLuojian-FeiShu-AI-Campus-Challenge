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


async def search_wiki(keywords: list[str]) -> list[WikiDoc]:
    """Search wiki for each keyword, deduplicate by node_token."""
    seen: set[str] = set()
    docs: list[WikiDoc] = []

    for kw in keywords:
        try:
            results = await _search_one(kw)
            for doc in results:
                if doc.node_token not in seen:
                    seen.add(doc.node_token)
                    docs.append(doc)
                    if len(docs) >= settings.WIKI_MAX_DOCS:
                        return docs
        except Exception as e:
            logger.warning("Wiki search failed for '%s': %s", kw, e)

    return docs


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

import json
import logging
import re

from app.config import settings
from app.lark import run_lark
from app.llm import call_llm
from app.models import ActionItem, WikiDoc

logger = logging.getLogger(__name__)

WIKI_LINK_SYSTEM = """You are a knowledge base curator.
Given an action item from a meeting and a list of candidate wiki documents,
select the 1-3 most relevant documents that would help the assignee complete this task.

Return ONLY a JSON array of node_token strings from the candidates list.
If none are relevant, return [].
Example: ["token_abc", "token_xyz"]
"""


async def enrich_with_wiki_links(items: list[ActionItem]) -> list[ActionItem]:
    """For each action item, search wiki and select relevant docs via LLM."""
    for item in items:
        try:
            item.wiki_links = await _find_links_for_item(item)
        except Exception as e:
            logger.warning("Wiki linking failed for '%s': %s", item.summary[:50], e)
    return items


async def _find_links_for_item(item: ActionItem) -> list[WikiDoc]:
    """Search wiki using action summary, then rank candidates with LLM."""
    params: dict = {"query": item.summary, "count": 8}
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

    candidates = [
        WikiDoc(
            title=n.get("title", ""),
            url=f"https://your-domain.feishu.cn/wiki/{n.get('node_token', '')}",
            space_name=n.get("space_name", ""),
            node_token=n.get("node_token", ""),
            excerpt=n.get("excerpt", ""),
        )
        for n in data.get("data", {}).get("items", [])
        if n.get("node_token")
    ]

    if not candidates:
        return []
    if len(candidates) == 1:
        return candidates

    # LLM reranks to top 1-3
    candidate_list = "\n".join(
        f"- node_token={d.node_token}: {d.title} ({d.space_name})" for d in candidates
    )
    user_msg = (
        f"Action item: {item.summary}\n"
        f"Context: {item.context}\n\n"
        f"Candidate documents:\n{candidate_list}"
    )

    raw = await call_llm(WIKI_LINK_SYSTEM, user_msg)
    try:
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if not match:
            return candidates[:2]
        selected_tokens = set(json.loads(match.group(0)))
        return [d for d in candidates if d.node_token in selected_tokens]
    except (json.JSONDecodeError, AttributeError):
        return candidates[:2]

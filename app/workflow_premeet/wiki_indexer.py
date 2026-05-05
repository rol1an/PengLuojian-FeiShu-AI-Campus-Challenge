"""Offline wiki index builder.

Proactively crawls the wiki space for recently-updated documents,
chunks and embeds them, then persists to doc_index + doc_embeddings.
This gives the vector recall path a candidate pool that is independent
of keyword search, enabling it to surface docs keyword search misses.

Entry point: rebuild_index() — called manually via /admin/rebuild-index
or automatically at startup when the index is empty.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta

from app.config import settings
from app.lark import run_lark
from app.models import WikiDoc
from app.workflow_premeet.vector_search import index_docs

logger = logging.getLogger(__name__)

_CONCURRENCY = 8          # concurrent doc-content fetches
_BROAD_QUERIES = [        # fallback queries when wiki nodes list unavailable
    " ", "项目", "方案", "文档", "会议", "设计", "报告", "计划",
    "总结", "进展", "规范", "架构", "接口", "需求",
]


async def rebuild_index(
    max_docs: int | None = None,
    recent_days: int | None = None,
) -> dict:
    """Crawl wiki and build the vector index.

    Returns {"indexed": N, "skipped": M, "errors": K}.
    """
    max_docs = max_docs or settings.INDEX_MAX_DOCS
    recent_days = recent_days or settings.INDEX_RECENT_DAYS
    cutoff_ts = time.time() - recent_days * 86400
    rebuild_start_ts = time.time()

    logger.info("rebuild_index: max_docs=%d recent_days=%d", max_docs, recent_days)

    # Step 1: collect candidate items from wiki
    raw_items = await _collect_candidates(max_docs, cutoff_ts)
    logger.info("rebuild_index: %d raw candidates collected", len(raw_items))

    if not raw_items:
        return {"indexed": 0, "skipped": 0, "errors": 0}

    # Step 2: fetch full content in batches and embed
    indexed = skipped = errors = 0
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def process_batch(batch: list[dict]) -> None:
        nonlocal indexed, skipped, errors
        docs = [_item_to_doc(item) for item in batch]
        obj_types = {item["node_token"]: item.get("obj_type", "docx") for item in batch if item.get("node_token")}

        # Fetch markdown content concurrently
        async def fetch_one(doc: WikiDoc) -> tuple[str, str]:
            async with sem:
                content = await _fetch_doc_content(doc.node_token)
                return doc.node_token, content

        content_results = await asyncio.gather(*[fetch_one(d) for d in docs], return_exceptions=True)
        full_contents: dict[str, str] = {}
        for res in content_results:
            if isinstance(res, Exception):
                errors += 1
                continue
            tok, content = res
            if content:
                full_contents[tok] = content
            else:
                skipped += 1

        try:
            await index_docs(docs, full_contents, obj_types)
            indexed += len(docs)
        except Exception as e:
            logger.warning("rebuild_index: batch index_docs failed: %s", e)
            errors += len(docs)

    # Process in batches of 20
    batch_size = 20
    for i in range(0, len(raw_items), batch_size):
        await process_batch(raw_items[i: i + batch_size])

    # Evict doc_index entries not refreshed in this run (stale/deleted docs)
    import app.persistence as _db
    removed = _db.doc_index_delete_stale(rebuild_start_ts)
    orphans = _db.doc_embeddings_delete_orphans()
    if removed:
        logger.info("rebuild_index: evicted %d stale doc_index entries, %d orphan embeddings", removed, orphans)

    logger.info(
        "rebuild_index done: indexed=%d skipped=%d errors=%d", indexed, skipped, errors
    )
    return {"indexed": indexed, "skipped": skipped, "errors": errors}


async def _collect_candidates(max_docs: int, cutoff_ts: float) -> list[dict]:
    """Gather candidate document items via wiki nodes list (primary) or docs +search (fallback)."""
    items = await _list_wiki_nodes(max_docs, cutoff_ts)
    if items:
        return items
    logger.info("rebuild_index: wiki nodes list unavailable, falling back to broad keyword search")
    return await _broad_keyword_search(max_docs, cutoff_ts)


async def _list_wiki_nodes(max_docs: int, cutoff_ts: float) -> list[dict]:
    """Use wiki nodes list API to get recently-updated nodes from the configured space."""
    if not settings.WIKI_SPACE_ID:
        logger.debug("rebuild_index: WIKI_SPACE_ID not set, skipping wiki nodes list")
        return []

    seen: set[str] = set()
    items: list[dict] = []

    try:
        data = await run_lark(
            "wiki", "nodes", "list",
            "--params", json.dumps({"space_id": settings.WIKI_SPACE_ID}),
            "--page-all",
            "--page-limit", "20",
            as_identity="user",
            timeout=30,
        )
    except Exception as e:
        logger.debug("_list_wiki_nodes failed: %s", e)
        return []

    nodes = data.get("data", {}).get("items", [])
    for node in nodes:
        node_token = node.get("node_token", "")
        if not node_token or node_token in seen:
            continue
        obj_edit_time = str(node.get("obj_edit_time", ""))
        # Filter by recency
        if obj_edit_time:
            try:
                if float(obj_edit_time) < cutoff_ts:
                    continue
            except (ValueError, TypeError):
                pass

        seen.add(node_token)
        items.append({
            "node_token": node_token,
            "obj_token": node.get("obj_token", node_token),
            "title": node.get("title", ""),
            "url": node.get("url", ""),
            "space_name": "",
            "owner": node.get("owner", "") or node.get("creator", ""),
            "creator": node.get("creator", "") or node.get("owner", ""),
            "obj_edit_time": obj_edit_time,
            "obj_type": node.get("obj_type", "docx").lower(),
            "excerpt": "",
        })
        if len(items) >= max_docs:
            break

    # Sort by recency descending
    items.sort(key=lambda x: float(x["obj_edit_time"]) if x["obj_edit_time"] else 0, reverse=True)
    return items[:max_docs]


async def _broad_keyword_search(max_docs: int, cutoff_ts: float) -> list[dict]:
    """Fallback: use broad keyword queries to discover docs across the space."""
    seen: set[str] = set()
    items: list[dict] = []

    for query in _BROAD_QUERIES:
        if len(items) >= max_docs:
            break
        try:
            data = await run_lark(
                "docs", "+search",
                "--query", query,
                "--page-size", "20",
                as_identity="user",
                timeout=settings.WIKI_SEARCH_TIMEOUT,
            )
        except Exception as e:
            logger.debug("_broad_keyword_search failed for '%s': %s", query, e)
            continue

        for r in data.get("data", {}).get("results", []):
            if r.get("entity_type") != "WIKI":
                continue
            meta = r.get("result_meta", {})
            node_token = meta.get("token", "")
            if not node_token or node_token in seen:
                continue

            obj_edit_time = str(meta.get("update_time", ""))
            if obj_edit_time:
                try:
                    if float(obj_edit_time) < cutoff_ts:
                        continue
                except (ValueError, TypeError):
                    pass

            raw_summary = r.get("summary_highlighted", "")
            excerpt = re.sub(r"</?h>", "", raw_summary).strip()
            seen.add(node_token)
            items.append({
                "node_token": node_token,
                "obj_token": node_token,
                "title": re.sub(r"</?h>", "", r.get("title_highlighted", node_token)).strip(),
                "url": meta.get("url", ""),
                "space_name": "",
                "owner": meta.get("owner_id", ""),
                "creator": meta.get("owner_id", ""),
                "obj_edit_time": obj_edit_time,
                "obj_type": meta.get("doc_types", "docx").lower(),
                "excerpt": excerpt,
            })
            if len(items) >= max_docs:
                break

    return items


async def _fetch_doc_content(node_token: str) -> str:
    """Fetch markdown content of a doc via lark-cli docs +fetch."""
    if not node_token:
        return ""
    try:
        data = await run_lark(
            "docs", "+fetch",
            "--doc", node_token,
            as_identity="user",
            timeout=20,
        )
        return data.get("data", {}).get("markdown", "")
    except Exception as e:
        logger.debug("_fetch_doc_content failed for %s: %s", node_token, e)
        return ""


def _item_to_doc(item: dict) -> WikiDoc:
    return WikiDoc(
        title=item.get("title", "Untitled"),
        url=item.get("url", ""),
        space_name=item.get("space_name", ""),
        node_token=item.get("node_token", ""),
        obj_token=item.get("obj_token", item.get("node_token", "")),
        excerpt=item.get("excerpt", ""),
        obj_edit_time=str(item.get("obj_edit_time", "")),
        owner=item.get("owner", "") or item.get("creator", ""),
        obj_type=item.get("obj_type", ""),
    )

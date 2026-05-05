import json
import logging
import re
import time

from app.config import settings
from app.lark import run_lark
from app.llm import call_llm
from app.models import WikiDoc

logger = logging.getLogger(__name__)

MEETING_ANALYSIS_SYSTEM = """你是一名新入职的秘书，需要在会议前为老板准备背景材料。
你没有任何历史上下文，需要从零开始思考这次会议。

给定会议标题和议程，请：
1. 判断会议类型（决策会/进展回顾/问题排查/项目启动/头脑风暴）
2. 推断这次会议"最想解决的 2-3 个核心问题"（不要复述标题，要推断会议背后真正需要解决的事）
3. 据此给出 4-6 个文档搜索词（比关键词更具体，能找到有决策价值的文档）
   - **第一条搜索词必须是会议标题中的专有名词/产品名/项目名原词（直接照抄，不得替换、意译或拼接额外修饰词），例如标题含"OpenClaw"则第一条就写"OpenClaw"，含"Trae IDE"则写"Trae IDE"**
   - 其余搜索词可以扩展语义，但至少一条包含来自标题的逐字短语
4. 总结"在近期聊天记录中最应该寻找什么信息"（一句话，指明信息类型，如进展/阻塞点/待确认事项）

Return ONLY valid JSON (no markdown fences):
{
  "meeting_type": "类型",
  "core_questions": ["核心问题1", "核心问题2"],
  "doc_search_queries": ["搜索词1", "搜索词2", "搜索词3", "搜索词4"],
  "im_focus": "在聊天记录中重点寻找的信息类型（一句话）"
}
"""

RERANK_SYSTEM = """You are a meeting preparation assistant.
Given a meeting title, agenda, and a list of candidate wiki documents,
score each document's relevance and value for participants to read before the meeting.

Each document includes:
- title: document title
- excerpt: document summary (up to 500 chars)
- space: knowledge space this document belongs to (same space as the meeting topic = more likely relevant)
- last_edited: how recently the document was last edited (more recent = more likely to reflect current state)

Return ONLY a JSON array of objects for up to {max_docs} most relevant documents, ordered by score descending:
[{{"node_token": "xxx", "score": 7.5, "reason": "one sentence"}}]

Score scale (0-10):
- 0-2: Clearly irrelevant
- 3-5: Weakly relevant
- 6-8: Relevant, useful background reading
- 9-10: Highly relevant, directly actionable before this meeting

Scoring guidance:
- Recently edited (<7 days) AND title matches meeting topic → prefer higher scores
- Template/reference docs (usually older, generic titles) → prefer lower scores
- space matches the meeting's project/team → slight boost

Only include documents with score >= 3. Omit clearly irrelevant ones.
Authority/role match is already factored in — focus on content relevance and decision value.
"""

_TEMPLATE_KEYWORDS = ("模板", "示例", "untitled", "test", "draft")


async def analyze_meeting(title: str, description: str = "") -> dict:
    """Analyze a meeting as a new secretary from scratch.

    Returns a dict with:
      - meeting_type: str
      - core_questions: list[str]
      - doc_search_queries: list[str]  (used as wiki search keywords)
      - im_focus: str  (passed to IM context selector)
    Falls back gracefully if LLM fails.
    """
    user_msg = f"会议标题：{title}\n议程/描述：{description or '（无）'}"
    raw = await call_llm(MEETING_ANALYSIS_SYSTEM, user_msg)
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("no JSON object found")
        data = json.loads(match.group(0))
        queries = data.get("doc_search_queries") or []
        if not queries:
            queries = [title]
        logger.info(
            "Meeting analysis for '%s': type=%s, queries=%s, im_focus=%s",
            title, data.get("meeting_type", "?"), queries, data.get("im_focus", ""),
        )
        return {
            "meeting_type": data.get("meeting_type", ""),
            "core_questions": data.get("core_questions", []),
            "doc_search_queries": queries,
            "im_focus": data.get("im_focus", ""),
        }
    except (json.JSONDecodeError, ValueError, AttributeError) as e:
        logger.warning("analyze_meeting failed (%s), falling back to title keywords", e)
        return {
            "meeting_type": "",
            "core_questions": [],
            "doc_search_queries": [title],
            "im_focus": "",
        }


async def search_wiki(
    keywords: list[str],
    title: str = "",
    description: str = "",
    attendee_open_ids: list[str] | None = None,
    organizer_open_id: str = "",
) -> list[WikiDoc]:
    """Search wiki, apply Layer 1 quality/relevance filtering, then Layer 2 LLM rerank."""
    if attendee_open_ids is None:
        attendee_open_ids = []

    seen: set[str] = set()
    raw_candidates: list[dict] = []
    max_candidates = settings.WIKI_MAX_DOCS * 2

    # Fallback: also search with noun chunks split from the meeting title,
    # so that verbatim title fragments always get a chance to hit wiki docs.
    all_keywords = list(keywords) + _title_chunks(title, existing=keywords)

    for kw in all_keywords:
        try:
            items = await _search_one_raw(kw)
            logger.info("Wiki raw hits for '%s': %s", kw, [i.get("title") for i in items])
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

    # ── Vector recall path (parallel to keyword, fails silently) ─────────────
    import app.persistence as _db
    if settings.VECTOR_SEARCH_ENABLED and _db.doc_index_count() > 0:
        try:
            from app.workflow_premeet.vector_search import embed_query, vector_recall
            q_vec = await embed_query(title, description, keywords)
            kw_tokens = {item["node_token"] for item in raw_candidates if item.get("node_token")}
            extra_items = await vector_recall(
                q_vec, exclude_tokens=kw_tokens, top_k=settings.VECTOR_TOP_K
            )
            if extra_items:
                raw_candidates.extend(extra_items)
                logger.info(
                    "Vector recall: +%d extra docs (index_size=%d)",
                    len(extra_items), _db.doc_index_count(),
                )
        except Exception as e:
            logger.warning("Vector search failed, using keyword-only candidates: %s", e)
    # ─────────────────────────────────────────────────────────────────────────

    # Layer 1: compute quality + relevance scores, filter by quality threshold
    scored: list[tuple[dict, float, float]] = []
    for item in raw_candidates:
        q = _quality_score(item)
        r = _relevance_score(
            item, keywords, title, attendee_open_ids,
            organizer_open_id=organizer_open_id,
        )
        if q >= settings.WIKI_QUALITY_THRESHOLD:
            scored.append((item, q, r))
        else:
            logger.info(
                "Layer1 filtered out '%s' (quality=%.2f)", item.get("title", "?"), q
            )

    if not scored:
        return []

    # Sort by combined score: quality * 0.4 + relevance * 0.6
    scored.sort(key=lambda x: x[1] * 0.4 + x[2] * 0.6, reverse=True)

    # Take top max_candidates for LLM
    top = scored[:max_candidates]
    candidates = [_item_to_doc(item) for item, _q, _r in top]

    # Dedup before rerank so duplicates don't consume slots
    candidates = _dedup_by_version(candidates)
    candidates = await _dedup_similar_docs(candidates)

    if len(candidates) <= settings.WIKI_MAX_DOCS:
        return candidates

    return await _rerank_with_llm(title, description, candidates, settings.WIKI_MAX_DOCS)


def _doc_to_rerank_item(doc: WikiDoc) -> dict:
    """Build a rich metadata dict for LLM rerank input."""
    item: dict = {"node_token": doc.node_token, "title": doc.title}
    if doc.excerpt:
        item["excerpt"] = doc.excerpt[:500]
    if doc.space_name:
        item["space"] = doc.space_name
    if doc.obj_edit_time:
        try:
            days_ago = int((time.time() - int(doc.obj_edit_time)) / 86400)
            item["last_edited"] = "今天" if days_ago == 0 else f"{days_ago}天前"
        except (ValueError, TypeError):
            pass
    return item


async def _rerank_with_llm(
    title: str,
    description: str,
    candidates: list[WikiDoc],
    max_docs: int,
) -> list[WikiDoc]:
    """Layer 2: LLM scores and reorders candidates; fills remainder if needed."""
    candidate_list = "\n".join(
        json.dumps(_doc_to_rerank_item(d), ensure_ascii=False)
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


DEDUP_SYSTEM = """You are a document deduplication assistant.
Given a list of documents (title + excerpt), identify groups of documents that are essentially the same document (same content, just stored in different locations or with slightly different titles).

Return ONLY valid JSON (no markdown fences):
{
  "duplicate_groups": [
    ["node_token_A", "node_token_B"],
    ...
  ]
}

Rules:
- Only group documents if their content is clearly the same (not just similar topics — truly the same document)
- Each group must have at least 2 node_tokens
- If no duplicates exist, return {"duplicate_groups": []}
"""


# Patterns that mark a version suffix — order matters: more specific first
_VERSION_SUFFIX_RE = re.compile(
    r"[\s\-_·]*("
    r"[vV]\d+(\.\d+)*"           # v1, v2, v1.0, v2.3.1
    r"|\d+\.\d+(\.\d+)*"         # 1.0, 1.1.0
    r"|第[一二三四五六七八九十百千]+版"  # 第一版, 第二版
    r"|[①②③④⑤⑥⑦⑧⑨⑩]"         # circled numbers
    r"|1️⃣|2️⃣|3️⃣|4️⃣|5️⃣|6️⃣|7️⃣|8️⃣|9️⃣"  # keycap emoji
    r"|\(\d+\)"                   # (1), (2)
    r"|_\d+|\s\d+$"              # _1, _2, trailing space+digit
    r")$"
)


def _base_title(title: str) -> str:
    """Strip version suffix and return the base title for grouping."""
    return _VERSION_SUFFIX_RE.sub("", title).strip()


def _version_sort_key(title: str) -> tuple:
    """Extract a sortable version key from a title suffix.

    Returns a tuple so that higher versions sort last (we want max).
    Examples: v2 → (2,), v1.2.3 → (1,2,3), 第二版 → (2,), 2️⃣ → (2,), no version → (0,)
    """
    _CN_NUM = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
    _EMOJI_NUM = {"1️⃣":1,"2️⃣":2,"3️⃣":3,"4️⃣":4,"5️⃣":5,"6️⃣":6,"7️⃣":7,"8️⃣":8,"9️⃣":9}
    _CIRCLE_NUM = {"①":1,"②":2,"③":3,"④":4,"⑤":5,"⑥":6,"⑦":7,"⑧":8,"⑨":9,"⑩":10}

    m = _VERSION_SUFFIX_RE.search(title)
    if not m:
        return (0,)
    token = m.group(1).strip()

    # v2, v1.2.3, 1.0, 1.1.0
    digits = re.findall(r"\d+", token)
    if digits:
        return tuple(int(d) for d in digits)
    # 第X版
    cn = re.search(r"第([一二三四五六七八九十]+)版", token)
    if cn:
        return (_CN_NUM.get(cn.group(1), 0),)
    # emoji / circled
    for d, v in {**_EMOJI_NUM, **_CIRCLE_NUM}.items():
        if d in token:
            return (v,)
    return (0,)


def _dedup_by_version(docs: list[WikiDoc]) -> list[WikiDoc]:
    """Rule-based: group docs by base title, keep only the highest-versioned doc in each group."""
    from collections import defaultdict
    groups: dict[str, list[WikiDoc]] = defaultdict(list)
    for doc in docs:
        groups[_base_title(doc.title)].append(doc)

    result: list[WikiDoc] = []
    for base, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
            continue
        group.sort(key=lambda d: _version_sort_key(d.title), reverse=True)
        result.append(group[0])
        for old in group[1:]:
            logger.info(
                "Version-dedup: keeping '%s', removing '%s'",
                group[0].title, old.title,
            )
    # Preserve original ordering for non-duplicate docs
    order = {d.node_token: i for i, d in enumerate(docs)}
    result.sort(key=lambda d: order.get(d.node_token, 0))
    return result


async def _dedup_similar_docs(docs: list[WikiDoc]) -> list[WikiDoc]:
    """Remove near-duplicate docs via LLM; for each duplicate group keep the most recently edited."""
    if len(docs) <= 1:
        return docs
    doc_list = "\n".join(
        f"- node_token={d.node_token}: {d.title}" + (f" | {d.excerpt[:100]}" if d.excerpt else "")
        for d in docs
    )
    try:
        raw = await call_llm(DEDUP_SYSTEM, f"Documents:\n{doc_list}")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return docs
        data = json.loads(match.group(0))
        groups: list[list[str]] = data.get("duplicate_groups", [])
        if not groups:
            return docs

        # For each duplicate group, keep only the doc with latest obj_edit_time
        tokens_to_remove: set[str] = set()
        for group in groups:
            group_docs = [d for d in docs if d.node_token in group]
            if len(group_docs) < 2:
                continue
            # Sort by edit time descending; keep first, remove rest
            def _edit_ts(d: WikiDoc) -> int:
                try:
                    return int(d.obj_edit_time)
                except (ValueError, TypeError):
                    return 0
            group_docs.sort(key=_edit_ts, reverse=True)
            for dup in group_docs[1:]:
                tokens_to_remove.add(dup.node_token)
                logger.info("Dedup: removing '%s' (older duplicate of '%s')", dup.title, group_docs[0].title)

        return [d for d in docs if d.node_token not in tokens_to_remove]
    except Exception as e:
        logger.debug("_dedup_similar_docs failed, skipping: %s", e)
        return docs


async def _search_one_raw(keyword: str) -> list[dict]:
    """Full-text search wiki/docs via docs +search (Search v2), return normalised items."""
    args = ["docs", "+search", "--query", keyword, "--page-size", "10"]

    data = await run_lark(
        *args,
        as_identity="user",
        timeout=settings.WIKI_SEARCH_TIMEOUT,
    )
    results = data.get("data", {}).get("results", [])

    items: list[dict] = []
    for r in results:
        if r.get("entity_type") != "WIKI":
            continue
        meta = r.get("result_meta", {})
        # Strip <h>...</h> highlight tags from summary
        raw_summary = r.get("summary_highlighted", "")
        excerpt = re.sub(r"</?h>", "", raw_summary).strip()
        items.append({
            "node_token": meta.get("token", ""),
            "obj_token": meta.get("token", ""),   # same token for docs +search
            "title": re.sub(r"</?h>", "", r.get("title_highlighted", meta.get("token", ""))).strip(),
            "url": meta.get("url", ""),
            "space_name": "",
            "owner": meta.get("owner_id", ""),
            "creator": meta.get("owner_id", ""),
            "obj_edit_time": str(meta.get("update_time", "")),
            "obj_type": meta.get("doc_types", "").lower(),
            "excerpt": excerpt,
        })
    return items


def _item_to_doc(item: dict) -> WikiDoc:
    return WikiDoc(
        title=item.get("title", "Untitled"),
        url=item.get("url") or _build_wiki_url(item.get("node_token", "")),
        space_name=item.get("space_name", ""),
        node_token=item.get("node_token", ""),
        obj_token=item.get("obj_token", ""),
        excerpt=item.get("excerpt", ""),
        obj_edit_time=str(item.get("obj_edit_time", "")),
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
    organizer_open_id: str = "",
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

    # Organizer is owner/creator of the doc
    if organizer_open_id and organizer_open_id in (owner, creator):
        score += 0.2

    return score


def _title_chunks(title: str, existing: list[str]) -> list[str]:
    """Derive extra search terms from the meeting title as a fallback.

    Strategy:
    1. Always include the raw title (truncated to 15 chars) so full-text search
       can match documents that contain any fragment of it.
    2. If the title contains delimiters, also add each segment as a separate term.

    Skips terms that are identical to (or fully contained in) an existing keyword.
    """
    if not title:
        return []
    existing_lower = [kw.lower() for kw in existing]
    chunks: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        t = term.strip()
        if len(t) < 2 or t.lower() in seen:
            return
        # Skip if an existing keyword already equals this term
        if t.lower() in existing_lower:
            return
        seen.add(t.lower())
        chunks.append(t)

    # 1. Delimiter-split segments (handles titles like "飞书AI—产品专场·技术架构")
    segments = re.split(r"[—\-·/ 　\t]+", title)
    for part in segments:
        _add(part)

    # 2. For segments that are still long (no useful delimiters found, e.g. pure Chinese),
    #    generate 4-char sliding-window chunks so full-text search can match shorter phrases.
    for seg in segments:
        # Strip ASCII/spaces from segment for Chinese-only chunking
        chinese_only = re.sub(r"[A-Za-z0-9\s]+", "", seg)
        if len(chinese_only) >= 8:  # Only worth chunking if long enough
            for i in range(0, len(chinese_only) - 3, 2):
                _add(chinese_only[i : i + 4])

    return chunks


def _build_wiki_url(node_token: str) -> str:
    return f"https://your-domain.feishu.cn/wiki/{node_token}"

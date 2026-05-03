import json
import logging
import re
from datetime import timezone, timedelta

from app.exceptions import LLMError
from app.lark import run_lark
from app.llm import call_llm
from app.models import CalendarEvent, MeetingBrief
from app.workflow_qa.context_store import context_store

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

_NO_CONTEXT_REPLY = (
    "抱歉，我暂时没有找到您最近的会前简报记录。"
    "如果您刚收到会前同步卡，请稍等片刻后再试。"
)
_ERROR_REPLY = "抱歉，我暂时无法回答您的问题，请稍后再试。"

_QA_SYSTEM_PROMPT = """\
你是飞书会议助手。用户刚刚收到了一份会前同步卡，现在向你提问关于即将到来的会议的问题。
请根据下方提供的会议上下文，用自然的中文直接回答用户问题，像了解情况的秘书向上司简短汇报。

回答遵循隐含顺序：先说结论（直接给答案），再补来源（私聊说联系人，文档附链接），最后一句话收口（如有必要）。

规则：
1. 只根据提供的信息作答，不要编造会议内容或参会人
2. 来源内联在正文中，不要单独分区块罗列：
   - 来自知识库文档：在句子后附上文档链接，格式为 (链接)，不使用书名号
   - 来自私聊：说明具体联系人，如"据与张三的沟通，..."
   - 来自群聊：说明具体群名，如"产品讨论群里提到..."
3. 不使用 Markdown 符号（# * ** 等），纯文本格式
4. 不超过 150 字，信息少时一句话即可
5. 如果上下文中确实找不到，直接说"这块我这边没有相关记录"

示例（仅参考风格，不要照抄内容）：
Cat A 直接命中型会议召回率最高，Hit@3 达到 100%，主要原因是标题含有具体项目名或工具名 (https://example.feishu.cn/wiki/xxx)。据与程俊华的沟通，他也建议把标题写得具体一些。\
"""

# ---------------------------------------------------------------------------
# Section-based document extraction
# ---------------------------------------------------------------------------

# Matches common heading formats:
#   ## Markdown headings
#   一、二、三、 Chinese numeral headings
#   1. 2. 3、 Arabic numeral headings
#   （一）（二） Parenthesized Chinese numerals
_HEADING_RE = re.compile(
    r'(?m)^('
    r'#{1,4} .+'
    r'|[一二三四五六七八九十百]+[、.．:：].+'
    r'|\d+[.．、:：][^\n]+'
    r'|（[一二三四五六七八九十]+）.+'
    r')$'
)

# High-frequency generic words that appear in almost every document
# and are not useful for discriminating between sections
_STOPWORDS = {
    "文档", "会议", "结果", "内容", "相关", "说明", "分析", "总结",
    "问题", "方案", "情况", "信息", "系统", "功能", "数据", "管理",
    "处理", "进行", "可以", "需要", "我们", "这个", "以及",
}


def _extract_terms(question: str) -> set[str]:
    """Extract discriminative search terms from a question.

    Strategy:
    - English / mixed tokens (e.g. RAG, Hit@3, Cat A): keep whole, don't split
    - Chinese segments: 2/3/4-gram + full segment for proper nouns
    - Filter stopwords to avoid inflating scores on generic terms
    """
    terms: set[str] = set()

    # English and alphanumeric tokens — preserve intact
    for word in re.findall(r'[A-Za-z][A-Za-z0-9@_\-]*|[0-9]+[A-Za-z@]+', question):
        if len(word) >= 1:
            terms.add(word.lower())

    # Chinese segments: n-gram (2/3/4) + full segment
    for seg in re.findall(r'[\u4e00-\u9fff]+', question):
        for n in (2, 3, 4):
            for i in range(len(seg) - n + 1):
                chunk = seg[i:i + n]
                if chunk not in _STOPWORDS:
                    terms.add(chunk)
        if len(seg) >= 2 and seg not in _STOPWORDS:
            terms.add(seg)

    return terms


def _score_section(heading: str, body: str, terms: set[str]) -> float:
    """Score a section by keyword overlap. Title hits count 3× body hits."""
    if not terms:
        return 0.0
    h = heading.lower()
    b = body.lower()
    title_hits = sum(1 for t in terms if t in h)
    body_hits = sum(1 for t in terms if t in b)
    return (title_hits * 3 + body_hits) / (len(terms) + 1)


def _extract_relevant_sections(
    markdown: str,
    question: str,
    max_chars: int = 3000,
    doc_title: str = "",
) -> str:
    """Return the most question-relevant sections of a markdown document.

    Always includes the first section (document background/intro).
    Remaining budget is filled by sections ranked by keyword relevance.
    Falls back to positional truncation for plain-text docs without headings.
    """
    if not markdown:
        return ""
    if len(markdown) <= max_chars:
        return markdown

    # Split by heading lines
    parts = _HEADING_RE.split(markdown)
    # parts: [pre_text, heading1, body1, heading2, body2, ...]

    if len(parts) == 1:
        # No recognizable headings — fall back to positional truncation
        logger.debug("section_extract doc='%s': no headings found, truncating", doc_title)
        return markdown[:max_chars]

    sections: list[tuple[str, str]] = []  # (heading, body)
    pre_text = parts[0].strip()
    if pre_text:
        sections.append(("", pre_text))
    for i in range(1, len(parts) - 1, 2):
        heading = parts[i].strip()
        body = parts[i + 1].strip() if (i + 1) < len(parts) else ""
        sections.append((heading, body))

    if not sections:
        return markdown[:max_chars]

    terms = _extract_terms(question)
    if not terms:
        logger.debug("section_extract doc='%s': no terms extracted, truncating", doc_title)
        return markdown[:max_chars]

    # First section: always include, cap at 1000 chars
    first_h, first_b = sections[0]
    first_text = (f"{first_h}\n{first_b}" if first_h else first_b).strip()
    first_snippet = first_text[:1000]
    remaining = max_chars - len(first_snippet) - 2  # -2 for "\n\n"

    if remaining <= 0:
        return first_snippet

    # Score and sort remaining sections by relevance
    other = [
        (_score_section(h, b, terms), idx, h, b)
        for idx, (h, b) in enumerate(sections)
        if idx > 0
    ]
    other_sorted = sorted(other, key=lambda x: (-x[0], x[1]))

    selected: list[tuple[int, str, str]] = []
    used = 0
    for score, idx, h, b in other_sorted:
        section_text = (f"{h}\n{b}" if h else b).strip()
        if not section_text:
            continue
        if used + len(section_text) + 2 <= remaining:
            selected.append((idx, h, b))
            used += len(section_text) + 2
        elif score > 0 and used < remaining:
            # Relevant section too large: include as much as fits
            leftover = remaining - used
            selected.append((idx, h, b[:leftover]))
            used = remaining
            break

    # Restore original document order
    selected.sort(key=lambda x: x[0])

    selected_headings = [h for _, h, _ in selected if h]
    total_chars = len(first_snippet) + sum(
        len((f"{h}\n{b}" if h else b).strip()) + 2
        for _, h, b in selected
    )
    logger.info(
        "section_extract doc='%s' question='%s' total_sections=%d selected=%s used_chars=%d",
        doc_title, question[:40], len(sections), selected_headings, total_chars,
    )

    result_parts = [first_snippet]
    for _, h, b in selected:
        result_parts.append((f"{h}\n{b}" if h else b).strip())
    return "\n\n".join(result_parts)


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def _build_context_block(event: CalendarEvent, brief: MeetingBrief) -> str:
    # Meeting info
    start_cst = event.start_time.astimezone(_CST).strftime("%Y-%m-%d %H:%M")
    attendee_names = [
        event.attendee_names.get(uid, uid) for uid in event.attendee_open_ids
    ]
    description = (event.description or "未填写")[:300]

    lines = [
        "# 会议信息",
        f"- 标题：{event.title}",
        f"- 时间：{start_cst}（北京时间）",
        f"- 参会人：{'、'.join(attendee_names) or '未知'}",
        f"- 议程：{description}",
        "",
    ]

    # One-line summary
    if brief.one_line:
        lines += [f"# 会议核心一句话", brief.one_line, ""]

    # IM context bullets — include source_label so LLM can cite specifically
    if brief.context_bullets:
        lines.append("# 近期沟通摘要")
        for bullet in brief.context_bullets:
            lines.append(f"- {bullet.text}  [来源：{bullet.source_label}]")
        lines.append("")

    # Key docs — include title + url + conclusions
    if brief.key_docs:
        lines.append("# 关键参考文档")
        for i, doc in enumerate(brief.key_docs, 1):
            link = doc.anchor_url or doc.url
            lines.append(f"{i}. 文档标题：{doc.title}")
            lines.append(f"   链接：{link}")
            if doc.why_relevant:
                lines.append(f"   与本次会相关：{doc.why_relevant}")
            if doc.key_conclusions:
                lines.append(f"   关键结论：{'；'.join(doc.key_conclusions)}")
            if doc.open_questions:
                lines.append(f"   待确认：{'；'.join(doc.open_questions)}")
            if doc.excerpt:
                lines.append(f"   文档原文节选：\n{doc.excerpt}")
        lines.append("")

    # Open questions
    if brief.open_questions:
        lines.append("# 待确认问题")
        for q in brief.open_questions:
            lines.append(f"- {q}")
        lines.append("")

    return "\n".join(lines)


async def _live_search_for_question(question: str, meeting_title: str) -> str:
    """Do a targeted wiki search for the user's question and return a context snippet."""
    try:
        from app.workflow_premeet.wiki_service import _search_one_raw, _quality_score
        from app.workflow_premeet.doc_enricher import _fetch_doc_content

        # Use the question as the search query directly
        results = await _search_one_raw(question[:50])
        if not results:
            return ""

        # Filter by basic quality and take top 2
        scored = [(r, _quality_score(r)) for r in results if _quality_score(r) >= 0.1]
        scored.sort(key=lambda x: x[1], reverse=True)
        top_docs = scored[:2]

        lines = ["# 实时检索补充（针对您的问题）"]
        for item, _ in top_docs:
            title = item.get("title", "")
            url = item.get("url", "")
            obj_token = item.get("obj_token", "")
            lines.append(f"\n## 文档：{title}")
            lines.append(f"链接：{url}")
            if obj_token:
                try:
                    full_text = await _fetch_doc_content(obj_token)
                    snippet = _extract_relevant_sections(
                        full_text, question, max_chars=3000, doc_title=title
                    )
                    lines.append(f"文档内容节选：\n{snippet}")
                except Exception:
                    excerpt = item.get("excerpt", "")
                    if excerpt:
                        lines.append(f"摘要：{excerpt}")
            else:
                excerpt = item.get("excerpt", "")
                if excerpt:
                    lines.append(f"摘要：{excerpt}")

        return "\n".join(lines)
    except Exception as e:
        logger.debug("Live wiki search for Q&A failed: %s", e)
        return ""


async def _ask_llm(event: CalendarEvent, brief: MeetingBrief, question: str) -> str:
    context_block = _build_context_block(event, brief)
    live_search = await _live_search_for_question(question, event.title)
    if live_search:
        context_block += f"\n\n{live_search}"
    user_message = f"{context_block}\n---\n用户问题：{question}"
    return await call_llm(_QA_SYSTEM_PROMPT, user_message, temperature=0.3)


async def _send_text_reply(open_id: str, text: str) -> None:
    try:
        await run_lark(
            "im", "+messages-send",
            "--user-id", open_id,
            "--msg-type", "text",
            "--content", json.dumps({"text": text}, ensure_ascii=False),
            as_identity="bot",
            no_format=True,
        )
    except Exception as e:
        logger.error("Failed to send Q&A reply to %s: %s", open_id, e)


async def handle_user_message(sender_open_id: str, text: str) -> None:
    """Entry point called by message_listener for each incoming DM."""
    entry = context_store.get(sender_open_id)

    if entry is None:
        logger.info("No meeting context for %s, sending fallback reply", sender_open_id)
        await _send_text_reply(sender_open_id, _NO_CONTEXT_REPLY)
        return

    try:
        reply = await _ask_llm(entry.event, entry.brief, text)
        logger.info(
            "Q&A answered for %s (event=%s): %s",
            sender_open_id, entry.event.event_id, reply[:60],
        )
    except LLMError as e:
        logger.error("LLM error for Q&A (user=%s): %s", sender_open_id, e)
        reply = _ERROR_REPLY

    await _send_text_reply(sender_open_id, reply)

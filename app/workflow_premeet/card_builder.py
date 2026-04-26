from datetime import timezone, timedelta

from app.models import CalendarEvent, WikiDoc

_CST = timezone(timedelta(hours=8))


def build_knowledge_card(event: CalendarEvent, docs: list[WikiDoc]) -> dict:
    """Basic pre-meeting knowledge card (title + space_name + link)."""
    doc_elements = _basic_doc_elements(docs)
    start_str = event.start_time.astimezone(_CST).strftime("%H:%M")

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"会前阅读 — {event.title}",
            },
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"您的会议 **{event.title}** 将于 **{start_str}** 开始。\n"
                        "以下是知识库中最相关的参考文档："
                    ),
                },
            },
            {"tag": "hr"},
            *doc_elements,
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "由飞书会议助手自动生成 · Powered by Claude",
                    }
                ],
            },
        ],
    }

    return {"msg_type": "interactive", "card": card}


def build_enriched_knowledge_card(event: CalendarEvent, docs: list[WikiDoc]) -> dict:
    """Enriched pre-meeting card with why_relevant / conclusions / questions per doc."""
    doc_elements: list[dict] = []
    start_str = event.start_time.astimezone(_CST).strftime("%H:%M")

    for i, doc in enumerate(docs, start=1):
        link = doc.anchor_url or doc.url
        lines = [f"**{i}. [{doc.title}]({link})**"]

        if doc.why_relevant:
            lines.append(f"与本次会相关：{doc.why_relevant}")
        if doc.key_conclusions:
            lines.append("关键结论：" + " · ".join(doc.key_conclusions))
        if doc.open_questions:
            lines.append("待确认：" + " · ".join(doc.open_questions))
        if not (doc.why_relevant or doc.key_conclusions):
            lines.append(doc.space_name or doc.excerpt or "")

        doc_elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(lines)},
            }
        )
        if i < len(docs):
            doc_elements.append({"tag": "hr"})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"会前阅读（精华版）— {event.title}",
            },
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"您的会议 **{event.title}** 将于 **{start_str}** 开始。\n"
                        "以下是知识库中最相关的参考文档（已提取关键信息）："
                    ),
                },
            },
            {"tag": "hr"},
            *doc_elements,
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "由飞书会议助手自动生成 · Powered by Claude",
                    }
                ],
            },
        ],
    }

    return {"msg_type": "interactive", "card": card}


def build_low_confidence_card(event: CalendarEvent, docs: list[WikiDoc]) -> dict:
    """Low-confidence card (orange header) shown when relevance signals are weak."""
    doc_elements = _basic_doc_elements(docs)
    start_str = event.start_time.astimezone(_CST).strftime("%H:%M")

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"会前参考（低置信度）— {event.title}",
            },
            "template": "orange",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"您的会议 **{event.title}** 将于 **{start_str}** 开始。\n"
                        "以下文档与本次会议主题匹配度较低，**供参考，相关性待确认**。\n"
                        "如需更准确的推荐，请在会议描述中补充更多关键词后重新触发。"
                    ),
                },
            },
            {"tag": "hr"},
            *doc_elements,
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "由飞书会议助手自动生成 · Powered by Claude",
                    }
                ],
            },
        ],
    }

    return {"msg_type": "interactive", "card": card}


def build_no_docs_card(event: CalendarEvent) -> dict:
    """Fallback card when wiki search returns nothing."""
    start_str = event.start_time.astimezone(_CST).strftime("%H:%M")
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"即将开始 — {event.title}"},
                "template": "grey",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"您的会议 **{event.title}** 将于 **{start_str}** 开始。\n"
                            "未找到与本次会议主题相关的知识库文档。"
                        ),
                    },
                }
            ],
        },
    }


def _basic_doc_elements(docs: list[WikiDoc]) -> list[dict]:
    elements: list[dict] = []
    for i, doc in enumerate(docs, start=1):
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{i}. [{doc.title}]({doc.url})**"
                    + (f"\n{doc.excerpt}" if doc.excerpt else f"\n{doc.space_name}"),
                },
            }
        )
        if i < len(docs):
            elements.append({"tag": "hr"})
    return elements

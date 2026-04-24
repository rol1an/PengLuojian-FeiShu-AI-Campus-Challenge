from app.models import CalendarEvent, WikiDoc


def build_knowledge_card(event: CalendarEvent, docs: list[WikiDoc]) -> dict:
    """
    Build a Feishu Interactive Card JSON for pre-meeting knowledge push.

    Card structure:
    - Blue header with meeting title
    - Intro text (starts at HH:MM)
    - Up to WIKI_MAX_DOCS document links
    - Auto-generated footer note
    """
    doc_elements: list[dict] = []
    for i, doc in enumerate(docs, start=1):
        doc_elements.append(
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
            doc_elements.append({"tag": "hr"})

    start_str = event.start_time.strftime("%H:%M")

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


def build_no_docs_card(event: CalendarEvent) -> dict:
    """Fallback card when wiki search returns nothing."""
    start_str = event.start_time.strftime("%H:%M")
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

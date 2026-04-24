from app.workflow_premeet.card_builder import build_knowledge_card, build_no_docs_card


def test_knowledge_card_structure(sample_calendar_event, sample_wiki_docs):
    result = build_knowledge_card(sample_calendar_event, sample_wiki_docs)
    assert result["msg_type"] == "interactive"
    card = result["card"]
    assert card["header"]["template"] == "blue"
    assert "Q3 OKR 规划会" in card["header"]["title"]["content"]
    # Should have more than just the header text element
    assert len(card["elements"]) > 2


def test_no_docs_card_structure(sample_calendar_event):
    result = build_no_docs_card(sample_calendar_event)
    assert result["msg_type"] == "interactive"
    assert result["card"]["header"]["template"] == "grey"


def test_knowledge_card_contains_doc_links(sample_calendar_event, sample_wiki_docs):
    result = build_knowledge_card(sample_calendar_event, sample_wiki_docs)
    card_text = str(result)
    assert "token_abc" in card_text
    assert "Q3 产品路线图" in card_text

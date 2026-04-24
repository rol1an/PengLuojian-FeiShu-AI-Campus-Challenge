import pytest


@pytest.mark.asyncio
async def test_push_uses_idempotency_key(mocker, sample_calendar_event, sample_wiki_docs):
    mock_run = mocker.patch("app.workflow_premeet.push_service.run_lark")
    mock_run.return_value = {"message_id": "om_test"}

    from app.workflow_premeet.push_service import push_knowledge_to_participants
    results = await push_knowledge_to_participants(sample_calendar_event, sample_wiki_docs)

    assert results["ou_alice"] is True
    assert results["ou_bob"] is True
    assert mock_run.call_count == 2

    # Check idempotency key format
    for call in mock_run.call_args_list:
        args = call[0]
        assert "--idempotency-key" in args
        key_idx = list(args).index("--idempotency-key")
        key_val = args[key_idx + 1]
        assert key_val.startswith("premeet-evt_001-")


@pytest.mark.asyncio
async def test_push_continues_on_individual_failure(mocker, sample_calendar_event, sample_wiki_docs):
    from app.exceptions import LarkCLIError

    mock_run = mocker.patch("app.workflow_premeet.push_service.run_lark")
    mock_run.side_effect = [LarkCLIError("send failed"), {"message_id": "om_test"}]

    from app.workflow_premeet.push_service import push_knowledge_to_participants
    results = await push_knowledge_to_participants(sample_calendar_event, sample_wiki_docs)

    assert results["ou_alice"] is False
    assert results["ou_bob"] is True

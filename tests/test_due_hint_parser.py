from app.workflow_postmeet.task_service import _parse_due_hint


def test_today():
    result = _parse_due_hint("by EOD")
    assert result.startswith("date:")


def test_tomorrow():
    from datetime import datetime, timezone, timedelta
    result = _parse_due_hint("by tomorrow")
    expected_date = (datetime.now(tz=timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    assert result == f"date:{expected_date}"


def test_next_week():
    from datetime import datetime, timezone, timedelta
    result = _parse_due_hint("next week")
    expected_date = (datetime.now(tz=timezone.utc) + timedelta(weeks=1)).strftime("%Y-%m-%d")
    assert result == f"date:{expected_date}"


def test_unknown_falls_back_to_default():
    from datetime import datetime, timezone, timedelta
    from app.config import settings
    result = _parse_due_hint("sometime soon")
    expected_date = (
        datetime.now(tz=timezone.utc) + timedelta(days=settings.TASK_DEFAULT_DUE_DAYS)
    ).strftime("%Y-%m-%d")
    assert result == f"date:{expected_date}"


def test_chinese_today():
    result = _parse_due_hint("今天完成")
    assert result.startswith("date:")

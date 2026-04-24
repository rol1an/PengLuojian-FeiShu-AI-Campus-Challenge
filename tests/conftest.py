import pytest
from datetime import datetime, timezone


@pytest.fixture
def sample_calendar_event():
    from app.models import CalendarEvent
    return CalendarEvent(
        event_id="evt_001",
        title="Q3 OKR 规划会",
        description="讨论 Q3 目标与路线图",
        start_time=datetime(2026, 4, 24, 14, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 24, 15, 0, tzinfo=timezone.utc),
        attendee_open_ids=["ou_alice", "ou_bob"],
        organizer_open_id="ou_alice",
    )


@pytest.fixture
def sample_wiki_docs():
    from app.models import WikiDoc
    return [
        WikiDoc(
            title="Q3 产品路线图",
            url="https://your-domain.feishu.cn/wiki/token_abc",
            space_name="产品空间",
            node_token="token_abc",
            excerpt="2026 Q3 产品目标与里程碑",
        ),
        WikiDoc(
            title="OKR 制定指南",
            url="https://your-domain.feishu.cn/wiki/token_xyz",
            space_name="知识库",
            node_token="token_xyz",
            excerpt="如何撰写高质量 OKR",
        ),
    ]

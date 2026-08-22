"""Live proof that "tomorrow at 3pm" resolves to 15:00 IST (UC10, O6).

Makes a real Haiku call, so it is opt-in:

    pytest -m live

Everything else in the suite stubs the model.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.parse import parse_capture

IST = timezone(timedelta(hours=5, minutes=30))

pytestmark = pytest.mark.live


async def test_tomorrow_at_3pm_is_1500_ist():
    """The user says 3pm; the stored instant must be 3pm in Kolkata."""
    now_ist = datetime(2026, 8, 22, 23, 15, tzinfo=IST)

    result = await parse_capture("Call the insurance guy tomorrow at 3pm", now=now_ist)

    assert result.due_at is not None, "Haiku returned no due_at"
    due_ist = result.due_at.astimezone(IST)

    assert (due_ist.hour, due_ist.minute) == (15, 0), f"got {due_ist.isoformat()}"
    assert due_ist.date() == now_ist.date() + timedelta(days=1)
    # 15:00 IST is 09:30Z — the tell that it was not resolved in UTC.
    assert result.due_at.astimezone(timezone.utc).strftime("%H:%M") == "09:30"
    assert result.state == "active"

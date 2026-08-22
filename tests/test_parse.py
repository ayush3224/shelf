"""Parse tests (UC9, UC10, UC12, UC14).

The model call is stubbed: these assert what *we* do with a reply, not what
Haiku decides. The one live check that Haiku really resolves "tomorrow at
3pm" against IST is in `tests/test_timezone_live.py`.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend import parse
from backend.parse import ParseError, ParseResult, _capture_tz, _decode, parse_capture

IST = timezone(timedelta(hours=5, minutes=30))


def _reply(text: str) -> SimpleNamespace:
    """Build a stand-in for an Anthropic Message carrying `text`."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
    )


@pytest.fixture
def stub_model(monkeypatch):
    """Stub the Anthropic call; return a dict recording the prompt it saw."""

    def _install(reply_json: str) -> dict:
        seen: dict = {}

        async def fake_create(**kwargs):
            seen.update(kwargs)
            return _reply(reply_json)

        monkeypatch.setattr(
            parse,
            "_client",
            lambda: SimpleNamespace(messages=SimpleNamespace(create=fake_create)),
        )
        return seen

    return _install


# ------------------------------------------------------------------ timezone


def test_capture_tz_is_ist():
    """TZ=Asia/Kolkata in .env means the parse resolves dates in IST (O6)."""
    offset = (
        datetime(2026, 8, 23, tzinfo=timezone.utc).astimezone(_capture_tz()).utcoffset()
    )
    assert offset == timedelta(hours=5, minutes=30)


def test_unknown_timezone_falls_back_to_utc(monkeypatch):
    """A typo'd TZ must not take the capture path down with it."""
    monkeypatch.setattr(parse.settings, "capture_timezone", "Mars/Olympus_Mons")
    assert datetime(2026, 8, 23, tzinfo=timezone.utc).astimezone(
        _capture_tz()
    ).utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_tomorrow_3pm_resolves_to_1500_ist(stub_model):
    """ "Tomorrow at 3pm" lands at 15:00 IST, not 15:00 UTC.

    The reference time handed to the model is IST, and a naive reply is
    interpreted as IST — so 3pm means 3pm where the user is (09:30 UTC).
    """
    now_ist = datetime(2026, 8, 22, 23, 7, tzinfo=IST)
    seen = stub_model(
        '{"kind":"task","text":"Call the insurance guy",'
        '"due_at":"2026-08-23T15:00:00","critical":false,'
        '"project_hint":null,"entities":[]}'
    )

    result = await parse_capture("Call the insurance guy tomorrow at 3pm", now=now_ist)

    # The model was told the current time in IST, not UTC.
    prompt = seen["messages"][0]["content"]
    assert "+05:30" in prompt

    assert result.due_at is not None
    assert result.due_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert (result.due_at.hour, result.due_at.minute) == (15, 0)
    assert result.due_at.date() == now_ist.date() + timedelta(days=1)
    # Same instant, stated in UTC: 15:00 IST is 09:30Z.
    assert result.due_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") == (
        "2026-08-23 09:30"
    )


@pytest.mark.asyncio
async def test_default_reference_time_is_in_capture_tz(stub_model):
    """With no explicit `now`, the reference time still carries the IST offset."""
    seen = stub_model(
        '{"kind":"note","text":"x","due_at":null,'
        '"critical":false,"project_hint":null,"entities":[]}'
    )
    await parse_capture("some note")
    assert "+05:30" in seen["messages"][0]["content"]


def test_offset_in_reply_is_respected():
    """An explicit offset from the model wins over the configured timezone."""
    result = _decode(
        '{"kind":"task","text":"x","due_at":"2026-08-23T15:00:00Z",'
        '"critical":false,"project_hint":null,"entities":[]}',
        "x",
    )
    assert result.due_at == datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------- cost limits


@pytest.mark.asyncio
async def test_parse_stays_on_haiku_and_within_200_tokens(stub_model):
    """CLAUDE.md cost rules: Haiku only, max_tokens capped at 200."""
    seen = stub_model(
        '{"kind":"note","text":"x","due_at":null,'
        '"critical":false,"project_hint":null,"entities":[]}'
    )
    await parse_capture("some note")
    assert seen["model"] == "claude-haiku-4-5"
    assert seen["max_tokens"] == 200


# ------------------------------------------------------- state & contract


@pytest.mark.parametrize(
    "due_at,expected",
    [(datetime(2026, 8, 23, 15, 0, tzinfo=IST), "active"), (None, "shelved")],
)
def test_initial_state_follows_due_at(due_at, expected):
    """UC12: captured with a time is active, without is shelved."""
    assert ParseResult("task", "x", due_at, False, None, []).state == expected


def test_entities_are_extracted_but_filtered():
    """Well-formed entities survive; junk is dropped. Linking is UC44."""
    result = _decode(
        '{"kind":"person_note","text":"Ravi is moving","due_at":null,'
        '"critical":false,"project_hint":null,"entities":['
        '{"type":"person","name":"Ravi"},{"type":"place","name":"Berlin"},'
        '{"type":"spaceship","name":"Heart of Gold"},{"name":"no type"},"junk"]}',
        "raw",
    )
    assert result.entities == [
        {"type": "person", "name": "Ravi"},
        {"type": "place", "name": "Berlin"},
    ]


def test_markdown_fenced_reply_is_accepted():
    """Haiku sometimes wraps JSON in a fence despite being told not to."""
    fenced = (
        '```json\n{"kind":"task","text":"Buy milk","due_at":null,'
        '"critical":false,"project_hint":null,"entities":[]}\n```'
    )
    assert _decode(fenced, "raw").text == "Buy milk"


def test_unparseable_due_at_does_not_sink_the_parse():
    """A bad date degrades to null rather than discarding a good parse."""
    result = _decode(
        '{"kind":"task","text":"x","due_at":"next Tuesday-ish",'
        '"critical":false,"project_hint":null,"entities":[]}',
        "x",
    )
    assert result.due_at is None
    assert result.state == "shelved"


def test_missing_text_falls_back_to_the_raw_capture():
    result = _decode(
        '{"kind":"note","text":"","due_at":null,"critical":false,'
        '"project_hint":null,"entities":[]}',
        "  the original capture  ",
    )
    assert result.text == "the original capture"


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        "[1, 2, 3]",
        '{"kind":"reminder","text":"x","due_at":null}',
    ],
)
def test_unusable_replies_raise_parse_error(body):
    """UC42: a bad reply fails loudly so the row is flagged, never lost."""
    with pytest.raises(ParseError):
        _decode(body, "raw")


@pytest.mark.asyncio
async def test_truncated_reply_raises(monkeypatch):
    """A max_tokens truncation is a failed parse, not a half-parsed item."""

    async def fake_create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"kind":"task"')],
            stop_reason="max_tokens",
        )

    monkeypatch.setattr(
        parse,
        "_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=fake_create)),
    )
    with pytest.raises(ParseError, match="max_tokens"):
        await parse_capture("something long")


@pytest.mark.asyncio
async def test_api_failure_becomes_parse_error(monkeypatch):
    """UC42: an API outage must surface as ParseError so the row survives."""

    async def boom(**kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(
        parse,
        "_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=boom)),
    )
    with pytest.raises(ParseError, match="Anthropic call failed"):
        await parse_capture("buy milk")

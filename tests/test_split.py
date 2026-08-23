"""Multi-item splitting tests (UC4).

The model call is stubbed. What is asserted is the contract around it: that
the common path stays one cheap call, that the second call is the only one
allowed a larger budget (D19), and that a bad split degrades to one item
rather than losing the capture (UC42).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend import parse
from backend.config import settings
from backend.parse import (
    SPLIT_SYSTEM_PROMPT,
    ParseError,
    _decode,
    _decode_split,
    parse_split,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _reply(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
    )


@pytest.fixture
def stub_model(monkeypatch):
    """Stub the Anthropic call; return a dict recording the request."""

    def _install(reply_json: str, stop_reason: str = "end_turn") -> dict:
        seen: dict = {}

        async def fake_create(**kwargs):
            seen.update(kwargs)
            return _reply(reply_json, stop_reason)

        monkeypatch.setattr(
            parse,
            "_client",
            lambda: SimpleNamespace(messages=SimpleNamespace(create=fake_create)),
        )
        return seen

    return _install


TWO_ITEMS = """{"items":[
  {"kind":"task","text":"Call the bank","due_at":"2026-08-24T15:00:00+05:30",
   "critical":false,"project_hint":null,"entities":[]},
  {"kind":"note","text":"Ravi liked the new office","due_at":null,
   "critical":false,"project_hint":null,"entities":[{"type":"person","name":"Ravi"}]}
]}"""


# -------------------------------------------------------------- the split flag


def test_split_defaults_to_false():
    """A reply without the field must not accidentally trigger a second call."""
    result = _decode('{"kind":"task","text":"x","due_at":null}', "x")
    assert result.split is False


def test_split_is_read_from_the_reply():
    result = _decode('{"kind":"task","text":"x","due_at":null,"split":true}', "x")
    assert result.split is True


# ------------------------------------------------------------------- decoding


def test_a_split_becomes_one_result_per_item():
    results = _decode_split(TWO_ITEMS, "raw")
    assert [r.kind for r in results] == ["task", "note"]
    assert results[0].text == "Call the bank"


def test_each_item_gets_its_own_state():
    """UC12 applies per item: the timed one is active, the untimed one shelves."""
    results = _decode_split(TWO_ITEMS, "raw")
    assert [r.state for r in results] == ["active", "shelved"]


def test_an_item_without_a_time_does_not_inherit_one():
    results = _decode_split(TWO_ITEMS, "raw")
    assert results[1].due_at is None


def test_a_malformed_item_is_dropped_not_fatal():
    """Losing one item is bad; losing all of them because of one is worse."""
    body = """{"items":[
      {"kind":"nonsense","text":"bad"},
      {"kind":"task","text":"good","due_at":null}
    ]}"""
    results = _decode_split(body, "raw")
    assert [r.text for r in results] == ["good"]


def test_the_item_count_is_capped(monkeypatch):
    monkeypatch.setattr(settings, "max_split_items", 2)
    body = '{"items":[%s]}' % ",".join(
        '{"kind":"task","text":"t%d","due_at":null}' % i for i in range(5)
    )
    assert len(_decode_split(body, "raw")) == 2


@pytest.mark.parametrize(
    "body",
    [
        '{"items":[]}',
        '{"items":"not a list"}',
        "{}",
        "[]",
        '{"items":[{"kind":"nonsense"}]}',
    ],
)
def test_an_unusable_split_raises(body):
    """The caller catches this and keeps the single parse it already has."""
    with pytest.raises(ParseError):
        _decode_split(body, "raw")


def test_a_fenced_split_reply_is_still_read():
    fenced = "```json\n" + TWO_ITEMS + "\n```"
    assert len(_decode_split(fenced, "raw")) == 2


# ------------------------------------------------------------------- the call


async def test_split_uses_the_split_prompt_and_the_larger_budget(stub_model):
    seen = stub_model(TWO_ITEMS)
    await parse_split("call the bank tomorrow at three, and Ravi liked the office")

    assert seen["system"] == SPLIT_SYSTEM_PROMPT
    # D19: the common path stays at 200; only this second call gets more, and
    # an array of items does not fit in 200.
    assert seen["max_tokens"] == settings.max_split_tokens
    assert seen["max_tokens"] > settings.max_parse_tokens


async def test_split_stays_on_haiku(stub_model):
    """A cost rule, not a preference: this is classification, not reasoning."""
    seen = stub_model(TWO_ITEMS)
    await parse_split("two things")
    assert seen["model"] == settings.anthropic_model
    assert "haiku" in settings.anthropic_model


async def test_split_passes_a_reference_time(stub_model):
    """Relative dates resolve against the user's timezone, not the server's."""
    seen = stub_model(TWO_ITEMS)
    reference = datetime(2026, 8, 23, 9, 0, tzinfo=IST)
    await parse_split("two things", now=reference)
    assert reference.isoformat() in seen["messages"][0]["content"]


async def test_a_truncated_split_raises(stub_model):
    """Half a split written to the database is worse than one whole item."""
    stub_model('{"items":[{"kind":"task"', stop_reason="max_tokens")
    with pytest.raises(ParseError, match="truncated"):
        await parse_split("two things")


async def test_an_api_failure_raises(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        parse,
        "_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=boom)),
    )
    with pytest.raises(ParseError, match="split call failed"):
        await parse_split("two things")

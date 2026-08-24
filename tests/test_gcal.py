"""The Google Calendar client (UC43).

Two things are worth testing here and neither is "does it send a request".

The first is the **shape of the event**, because two of its fields are
deliberately not Google's defaults and both would fail silently: reminders off
(the app is the reminder system, and leaving them on notifies the owner twice
for every timed item), and transparent (these are moments, not meetings, and a
calendar that reports the owner as busy fifteen times a day is worse than no
calendar).

The second is the **reading of a failure**, for the same reason the push tests
care about it: the scheduler decides whether to retry, to recreate, or to give
up based on what this module says happened, and every one of those is wrong
for the other two cases.
"""

from datetime import datetime, timezone

import httpx
import pytest

from backend import gcal
from backend.config import settings


#: Captured before any fixture can replace them. `tests/conftest.py` stubs the
#: whole client out for every test in the suite, which is right everywhere
#: except here — this file is *about* the client.
_REAL = {
    name: getattr(gcal, name)
    for name in ("create_event", "patch_event", "delete_event", "access_token")
}


@pytest.fixture(autouse=True)
def the_real_client(monkeypatch):
    """Opt out of the global guard, safely.

    Nothing leaves the process regardless: every test here routes the client
    through a fake transport, and the two that do not are testing what happens
    when there is no key to sign with.
    """
    for name, function in _REAL.items():
        monkeypatch.setattr(gcal, name, function)


@pytest.fixture(autouse=True)
def no_cached_token():
    """No test inherits another's access token."""
    gcal.forget_token()
    yield
    gcal.forget_token()


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """A calendar id and a key file, so `enabled()` is true by default."""
    monkeypatch.setattr(settings, "google_calendar_id", "owner@example.com")
    monkeypatch.setattr(
        settings, "google_calendar_key_file", "google-calendar-key.json"
    )
    monkeypatch.setattr(settings, "capture_timezone", "Asia/Kolkata")


class Recorder(httpx.AsyncBaseTransport):
    """Answers each request from a queue and remembers what was asked."""

    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.pop(0)
        if isinstance(body, str):
            return httpx.Response(status, text=body, request=request)
        return httpx.Response(status, json=body, request=request)


@pytest.fixture
def transport(monkeypatch):
    """Route every call in `gcal` through a transport the test controls."""

    def install(responses: list[tuple[int, object]]) -> Recorder:
        recorder = Recorder(responses)
        original = httpx.AsyncClient

        def build(*args, **kwargs):
            kwargs["transport"] = recorder
            return original(*args, **kwargs)

        monkeypatch.setattr(gcal.httpx, "AsyncClient", build)
        return recorder

    return install


@pytest.fixture
def signed(monkeypatch):
    """Skip the RS256 signing; the key file is not the subject here."""
    monkeypatch.setattr(
        gcal,
        "_service_account",
        lambda: {
            "client_email": "shelf@example.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
            "token_uri": "https://oauth2.googleapis.com/token",
            "private_key_id": "kid",
        },
    )
    monkeypatch.setattr(gcal.jwt, "encode", lambda *a, **k: "assertion")


TOKEN_OK = (200, {"access_token": "tok", "expires_in": 3600})


def event(text: str = "Call the insurance guy") -> gcal.CalendarEvent:
    """One event, due at a fixed instant."""
    return gcal.CalendarEvent(
        item_id="11111111-1111-4111-8111-111111111111",
        text=text,
        due_at=datetime(2026, 8, 25, 9, 30, tzinfo=timezone.utc),
        raw_text="call the insurance guy tomorrow at three",
    )


# ------------------------------------------------------------ event shape


def test_event_is_not_a_second_reminder():
    """Google must not notify: the app already does (UC23)."""
    body = event().body()
    assert body["reminders"] == {"useDefault": False, "overrides": []}


def test_event_does_not_mark_the_owner_busy():
    """A due item is a moment, not a commitment."""
    assert event().body()["transparency"] == "transparent"


def test_event_runs_for_the_configured_length(monkeypatch):
    """Start is the due time; end is start plus the configured minutes."""
    monkeypatch.setattr(settings, "google_calendar_event_minutes", 15)
    body = event().body()
    start = datetime.fromisoformat(body["start"]["dateTime"])
    end = datetime.fromisoformat(body["end"]["dateTime"])

    assert start == datetime(2026, 8, 25, 9, 30, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 15 * 60
    assert body["start"]["timeZone"] == "Asia/Kolkata"


def test_event_carries_the_item_id():
    """A stray event months later has to be traceable to what made it."""
    body = event().body()
    private = body["extendedProperties"]["private"]
    assert private[gcal.ITEM_ID_PROPERTY] == "11111111-1111-4111-8111-111111111111"
    assert "11111111-1111-4111-8111-111111111111" in body["description"]


def test_description_keeps_the_transcript_when_it_differs():
    """The edit lands on `parsed_text` (D14); what was said is worth keeping."""
    assert "call the insurance guy tomorrow at three" in event().body()["description"]


def test_description_does_not_repeat_the_summary():
    """When the text was never corrected, the transcript adds nothing."""
    same = gcal.CalendarEvent(
        item_id="i", text="Pay rent", due_at=event().due_at, raw_text="Pay rent"
    )
    assert same.body()["description"] == "Shelf item i"


def test_a_blank_capture_still_gets_a_findable_title():
    """A failed parse is flagged, not hidden (UC42)."""
    blank = gcal.CalendarEvent(item_id="i", text="   ", due_at=event().due_at)
    assert blank.body()["summary"] == "Untitled Shelf item"


# ----------------------------------------------------------------- config


def test_disabled_without_a_calendar_id(monkeypatch):
    """No calendar configured is a quiet skip, not a failure every minute."""
    monkeypatch.setattr(settings, "google_calendar_id", "")
    assert gcal.enabled() is False


def test_enabled_when_configured():
    assert gcal.enabled() is True


# ------------------------------------------------------------------ auth


async def test_token_is_reused_within_its_lifetime(signed, transport):
    """One token exchange an hour, not one a minute."""
    recorder = transport([TOKEN_OK])
    assert await gcal.access_token() == "tok"
    assert await gcal.access_token() == "tok"
    assert len(recorder.requests) == 1


async def test_a_refused_assertion_is_not_retried(signed, transport):
    """A bad key is not a blip; retrying it hides the real problem."""
    transport([(401, {"error": "invalid_grant"})])
    with pytest.raises(gcal.CalendarError) as raised:
        await gcal.access_token()
    assert raised.value.retryable is False


async def test_a_missing_key_file_is_not_retryable(monkeypatch):
    """No amount of waiting fixes a path."""
    monkeypatch.setattr(settings, "google_calendar_key_file", "/nope/missing.json")
    with pytest.raises(gcal.CalendarError) as raised:
        await gcal.access_token()
    assert raised.value.retryable is False


# ------------------------------------------------------------- the calls


async def test_create_returns_the_event_id(signed, transport):
    recorder = transport([TOKEN_OK, (200, {"id": "evt-1"})])
    assert await gcal.create_event("owner@example.com", event()) == "evt-1"

    written = recorder.requests[-1]
    assert written.method == "POST"
    # The `@` has to be escaped or it ends the path segment early.
    assert "owner%40example.com" in str(written.url)


async def test_update_is_a_patch_not_a_replacement(signed, transport):
    """A PUT would erase anything the owner added on Google's side."""
    recorder = transport([TOKEN_OK, (200, {"id": "evt-1"})])
    await gcal.patch_event("owner@example.com", "evt-1", event())
    assert recorder.requests[-1].method == "PATCH"
    assert str(recorder.requests[-1].url).endswith("/events/evt-1")


async def test_deleting_an_event_that_is_already_gone_succeeds(signed, transport):
    """The caller wanted it absent and it is absent."""
    transport([TOKEN_OK, (404, "not found")])
    await gcal.delete_event("owner@example.com", "evt-1")


async def test_a_stale_token_is_retried_once(signed, transport):
    """A 401 mid-tick is a fresh token away, not a failed sync."""
    recorder = transport([TOKEN_OK, (401, "expired"), TOKEN_OK, (200, {"id": "evt-1"})])
    assert await gcal.create_event("owner@example.com", event()) == "evt-1"
    assert len(recorder.requests) == 4


# --------------------------------------------------------- reading errors


async def test_a_patched_event_that_vanished_is_reported_gone(signed, transport):
    """The scheduler recreates it; it must not read this as a plain failure."""
    transport([TOKEN_OK, (404, "not found")])
    with pytest.raises(gcal.CalendarError) as raised:
        await gcal.patch_event("owner@example.com", "evt-1", event())
    assert raised.value.gone is True


async def test_lost_sharing_is_not_retried(signed, transport):
    """A 403 that is not rate limiting means somebody has to fix something."""
    transport([TOKEN_OK, (403, '{"error": {"message": "forbidden"}}')])
    with pytest.raises(gcal.CalendarError) as raised:
        await gcal.create_event("owner@example.com", event())
    assert raised.value.retryable is False


async def test_rate_limiting_is_retried(signed, transport):
    """Google overloads 403 for quota, and that one is worth another go."""
    transport(
        [TOKEN_OK, (403, '{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}')]
    )
    with pytest.raises(gcal.CalendarError) as raised:
        await gcal.create_event("owner@example.com", event())
    assert raised.value.retryable is True


@pytest.mark.parametrize(
    "status,retryable", [(429, True), (500, True), (503, True), (400, False)]
)
async def test_retryability(signed, transport, status, retryable):
    transport([TOKEN_OK, (status, "nope")])
    with pytest.raises(gcal.CalendarError) as raised:
        await gcal.create_event("owner@example.com", event())
    assert raised.value.retryable is retryable


async def test_an_unreachable_google_is_retryable(signed, monkeypatch):
    """A network blip must not burn the row's last attempt as if it were fatal."""

    class Dead(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("no route", request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        gcal.httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": Dead()}),
    )
    with pytest.raises(gcal.CalendarError) as raised:
        await gcal.access_token()
    assert raised.value.retryable is True

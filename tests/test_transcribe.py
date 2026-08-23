"""Cloud transcription tests (UC8, UC42, D23).

The HTTP call is stubbed throughout: what matters is what we do with a reply,
including the replies that carry no confidence and the ones that fail.
"""

import math
import random

import httpx
import pytest

from backend import transcribe as transcribe_module
from backend.config import settings
from backend.transcribe import (
    Transcript,
    TranscriptionError,
    _confidence_from_segments,
    _decode,
    transcribe,
)


def segment(logprob: float, start: float = 0.0, end: float = 1.0, **extra):
    """One Whisper segment."""
    return {"avg_logprob": logprob, "start": start, "end": end, **extra}


# ------------------------------------------------------------------ confidence


def test_confidence_is_none_without_segments():
    """A host that ignores verbose_json gives no signal, which is not zero."""
    assert _confidence_from_segments(None) is None
    assert _confidence_from_segments([]) is None
    assert _confidence_from_segments("nonsense") is None


def test_confidence_is_the_exponent_of_a_single_logprob():
    """One segment, no silence: the score is just exp(avg_logprob)."""
    score = _confidence_from_segments([segment(math.log(0.8))])
    assert score == pytest.approx(0.8, abs=1e-6)


def test_confidence_is_weighted_by_duration():
    """A long confident passage is not outvoted by a short uncertain one."""
    score = _confidence_from_segments(
        [
            segment(math.log(0.9), start=0.0, end=9.0),
            segment(math.log(0.1), start=9.0, end=10.0),
        ]
    )
    # The unweighted mean would be 0.5; duration weighting keeps it near 0.9.
    assert score == pytest.approx((0.9 * 9 + 0.1 * 1) / 10, abs=1e-6)


def test_no_speech_probability_discounts_a_segment():
    """Confident-sounding silence is not a confident transcript."""
    assert _confidence_from_segments(
        [segment(math.log(0.9), no_speech_prob=0.9)]
    ) == pytest.approx(0.09, abs=1e-6)


def test_confidence_stays_in_range():
    """A model that reports a positive logprob must not produce a score above 1."""
    assert _confidence_from_segments([segment(5.0)]) == 1.0


def test_segments_without_a_logprob_are_skipped():
    """One malformed segment does not poison the score."""
    score = _confidence_from_segments([{"start": 0, "end": 1}, segment(math.log(0.5))])
    assert score == pytest.approx(0.5, abs=1e-6)


# ---------------------------------------------------------------------- decode


def test_decode_returns_text_and_marks_the_path():
    """The path is recorded on every transcript, not just the words."""
    result = _decode(
        {"text": "  call the bank  ", "segments": [segment(math.log(0.9))]}
    )
    assert result.text == "call the bank"
    assert result.source == "cloud"
    assert result.confidence == pytest.approx(0.9, abs=1e-6)


@pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": "   "}, "not a dict"])
def test_decode_rejects_a_reply_without_words(payload):
    """No text is a failure, so the caller keeps the audio and flags it."""
    with pytest.raises(TranscriptionError):
        _decode(payload)


# ------------------------------------------------------------- low_confidence


def test_low_confidence_is_relative_to_the_floor(monkeypatch):
    monkeypatch.setattr(settings, "transcript_confidence_floor", 0.75)
    assert Transcript("x", 0.6, "cloud").low_confidence is True
    assert Transcript("x", 0.9, "cloud").low_confidence is False


def test_the_floor_is_set_for_a_single_known_language():
    """D23 raised this with the language pin. If the language is ever cleared
    this has to come back down, or every non-English capture gets flagged."""
    assert settings.transcript_confidence_floor == 0.75


def test_missing_confidence_is_not_low_confidence():
    """Absent is not the same as bad; flagging it would flag every host that
    does not report segments."""
    assert Transcript("x", None, "cloud").low_confidence is False


# ----------------------------------------------------------------- transcribe


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Record backoff waits instead of serving them. No test sleeps."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(transcribe_module, "_sleep", fake_sleep)
    return waits


class FakeResponse:
    """Enough of an httpx.Response for the retry loop to inspect."""

    def __init__(self, status: int, json_body=None, headers=None):
        self.status_code = status
        self._json = json_body
        self.headers = headers or {}
        self.text = "boom"

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


@pytest.fixture
def stub_http(monkeypatch):
    """Stub httpx.AsyncClient.post; return a dict recording the last request."""

    def _install(*, status: int = 200, json_body=None, raises: bool = False):
        seen: dict = {}

        async def fake_post(self, url, **kwargs):
            seen.update({"url": url, **kwargs})
            seen["calls"] = seen.get("calls", 0) + 1
            if raises:
                raise httpx.ConnectError("no route to host")
            return FakeResponse(status, json_body)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        return seen

    return _install


@pytest.fixture
def stub_sequence(monkeypatch):
    """Stub the POST with a scripted list of responses or exceptions."""

    def _install(*responses):
        calls: list[dict] = []
        queue = list(responses)

        async def fake_post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            nxt = queue.pop(0) if queue else responses[-1]
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        return calls

    return _install


def ok(text: str = "hi"):
    return FakeResponse(200, {"text": text})


def rate_limited(retry_after: str | None = None):
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return FakeResponse(429, {}, headers)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")


async def test_transcribe_sends_the_audio_and_asks_for_segments(stub_http):
    seen = stub_http(json_body={"text": "hi", "segments": [segment(math.log(0.9))]})
    result = await transcribe(
        b"audio-bytes", filename="c.m4a", content_type="audio/m4a"
    )

    assert result.text == "hi"
    assert seen["url"].endswith("/audio/transcriptions")
    assert seen["data"]["model"] == settings.groq_model
    # Without verbose_json there are no segments, and no confidence at all.
    assert seen["data"]["response_format"] == "verbose_json"
    assert seen["files"]["file"] == ("c.m4a", b"audio-bytes", "audio/m4a")


async def test_transcribe_pins_the_language_by_default(stub_http):
    """D23: detection is a guess, and a wrong guess returns confident nonsense
    rather than an error."""
    seen = stub_http(json_body={"text": "hi"})
    await transcribe(b"x")
    assert seen["data"]["language"] == "en"


def test_the_configured_default_language_is_english():
    assert settings.groq_language == "en"


async def test_clearing_the_language_restores_detection(stub_http, monkeypatch):
    """The escape hatch in D23: blank means let Whisper decide again."""
    monkeypatch.setattr(settings, "groq_language", "")
    seen = stub_http(json_body={"text": "hi"})
    await transcribe(b"x")
    assert "language" not in seen["data"]


async def test_transcribe_without_a_key_fails_before_the_network(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "")
    with pytest.raises(TranscriptionError, match="GROQ_API_KEY is not configured"):
        await transcribe(b"x")


def test_the_endpoint_is_groq(stub_http=None):
    """D24. The path this appends is OpenAI-compatible, so the base URL is the
    only thing that names the provider."""
    assert settings.groq_api_base == "https://api.groq.com/openai/v1"


def test_the_model_is_a_whisper_variant():
    """The cost rules cap the *parse* at Haiku; transcription is a separate
    line item, and it must stay a Whisper model (D24)."""
    assert settings.groq_model.startswith("whisper-large-v3")


async def test_transcribe_refuses_empty_audio():
    with pytest.raises(TranscriptionError, match="empty"):
        await transcribe(b"")


async def test_transcribe_surfaces_a_refusal(stub_http):
    """A non-retryable refusal is final on the first attempt."""
    seen = stub_http(status=400, json_body={})
    with pytest.raises(TranscriptionError, match="refused"):
        await transcribe(b"x")
    assert seen["calls"] == 1


async def test_transcribe_surfaces_a_transport_failure(stub_http):
    stub_http(raises=True)
    with pytest.raises(TranscriptionError, match="Could not reach"):
        await transcribe(b"x")


async def test_transcribe_surfaces_a_non_json_reply(stub_http, monkeypatch):
    seen = stub_http(json_body=None)
    assert seen is not None

    class Broken:
        status_code = 200
        text = "<html>"

        def json(self):
            raise ValueError("nope")

    async def fake_post(self, url, **kwargs):
        return Broken()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(TranscriptionError, match="non-JSON"):
        await transcribe(b"x")


def test_module_never_reaches_for_anthropic():
    """The Haiku parse lives in parse.py; this path must not grow a model call."""
    assert not hasattr(transcribe_module, "AsyncAnthropic")


# --------------------------------------------------------------- retry (D25)


async def test_a_429_is_retried_and_can_succeed(stub_sequence, slept):
    """The whole point: a rate limit should cost a few seconds, not the words."""
    calls = stub_sequence(rate_limited(), ok("call the bank"))
    result = await transcribe(b"x")

    assert result.text == "call the bank"
    assert len(calls) == 2
    assert len(slept) == 1


async def test_the_server_s_retry_after_is_honoured(stub_sequence, slept):
    stub_sequence(rate_limited(retry_after="2.5"), ok())
    await transcribe(b"x")
    assert slept == [2.5]


async def test_an_absurd_retry_after_is_ignored(stub_sequence, slept):
    """A header asking for ten minutes is not something a capture can wait
    out; the deadline is the real bound, so fall back to backoff."""
    stub_sequence(rate_limited(retry_after="600"), ok())
    await transcribe(b"x")
    assert slept and slept[0] <= transcribe_module._BACKOFF_BASE_SECONDS


async def test_an_unparseable_retry_after_falls_back_to_backoff(stub_sequence, slept):
    stub_sequence(rate_limited(retry_after="Wed, 21 Oct 2026 07:28:00 GMT"), ok())
    await transcribe(b"x")
    assert slept and slept[0] <= transcribe_module._BACKOFF_BASE_SECONDS


async def test_backoff_grows_between_attempts(stub_sequence, slept, monkeypatch):
    """Full jitter, so assert the ceiling doubles rather than the exact value."""
    monkeypatch.setattr(random, "uniform", lambda a, b: b)
    stub_sequence(rate_limited(), rate_limited(), ok())
    await transcribe(b"x")
    assert slept == [
        transcribe_module._BACKOFF_BASE_SECONDS,
        transcribe_module._BACKOFF_BASE_SECONDS * 2,
    ]


async def test_retries_are_bounded(stub_sequence, slept):
    """Three attempts, then the capture keeps its audio and loses its words."""
    calls = stub_sequence(rate_limited())
    with pytest.raises(TranscriptionError, match="429"):
        await transcribe(b"x")

    assert len(calls) == transcribe_module._MAX_ATTEMPTS
    # One fewer sleep than attempts: no point waiting after the last try.
    assert len(slept) == transcribe_module._MAX_ATTEMPTS - 1


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_transient_statuses_are_retried(status, stub_sequence):
    calls = stub_sequence(FakeResponse(status, {}), ok())
    await transcribe(b"x")
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 415, 422])
async def test_settled_statuses_are_not_retried(status, stub_sequence, slept):
    """Bad audio, a bad key or a file too large will say the same thing twice.
    Retrying only makes the user wait for it."""
    calls = stub_sequence(FakeResponse(status, {}), ok())
    with pytest.raises(TranscriptionError):
        await transcribe(b"x")
    assert len(calls) == 1
    assert slept == []


async def test_a_dropped_connection_is_retried(stub_sequence, slept):
    """Same transient class as a 429."""
    calls = stub_sequence(httpx.ConnectError("reset by peer"), ok())
    result = await transcribe(b"x")
    assert result.text == "hi"
    assert len(calls) == 2


async def test_the_retry_budget_fits_inside_the_client_timeout():
    """The app gives up at 90s (mobile/lib/api.ts). Retrying past that turns a
    recoverable failure into one the app has already stopped waiting for."""
    assert transcribe_module._TOTAL_DEADLINE_SECONDS < 90


async def test_a_retry_that_would_outlast_the_deadline_is_not_taken(
    stub_sequence, slept, monkeypatch
):
    """Waiting 2s out of a 3s budget leaves too little for the attempt the
    wait was supposed to buy."""
    monkeypatch.setattr(transcribe_module, "_TOTAL_DEADLINE_SECONDS", 3.0)
    calls = stub_sequence(rate_limited(retry_after="2"))
    with pytest.raises(TranscriptionError):
        await transcribe(b"x")

    assert len(calls) == 1
    assert slept == []


async def test_time_already_spent_counts_against_the_budget(
    stub_sequence, slept, monkeypatch
):
    """The deadline is wall-clock, not a retry counter: a slow first attempt
    eats the budget the retries would have used."""
    # Reads 0.0 while the deadline is set, then jumps past it. It must not run
    # out: pytest's own teardown reads the clock too.
    readings = [0.0, 0.0, 74.0]

    def clock() -> float:
        return readings.pop(0) if len(readings) > 1 else readings[0]

    monkeypatch.setattr(transcribe_module.time, "monotonic", clock)
    calls = stub_sequence(rate_limited(), ok())
    with pytest.raises(TranscriptionError):
        await transcribe(b"x")

    assert len(calls) == 1
    assert slept == []


async def test_a_non_json_body_is_not_retried(stub_sequence, slept):
    """A 200 that is not JSON is a broken host, not a busy one."""
    calls = stub_sequence(FakeResponse(200, ValueError("nope")))
    with pytest.raises(TranscriptionError, match="non-JSON"):
        await transcribe(b"x")
    assert len(calls) == 1
    assert slept == []

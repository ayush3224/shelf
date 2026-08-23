"""Cloud transcription of a recording (UC8).

Whisper, served by Groq (D24). The endpoint is OpenAI-compatible
`/audio/transcriptions`, so moving to another host is a base-URL change and
nothing else. Nothing Anthropic goes through here; the Haiku parse is
`parse.py`.

A rate-limited or flaky attempt is retried with backoff before giving up
(D25). Giving up is survivable — the row and the audio are already written by
then — but it costs the capture its words, so it is worth a few seconds first.

This is the *only* transcription path the server has. The architecture doc
puts on-device `SpeechRecognizer` first with this as fallback, and the app
still sends an on-device transcript when it has one — see the note in
`docs/decisions.md` (D20) for why the app does not currently produce one.

The language is pinned rather than detected (D23).
"""

import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

# Turbo on a short clip is a second or two. This is the ceiling for one attempt
# before the capture is treated as untranscribed — the audio is already safe by
# then.
_TIMEOUT_SECONDS = 60.0

# Retries exist for one reason: a 429 should cost the capture its words only
# after we have actually tried to wait it out.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 8.0

# The whole call, retries included, must finish inside the client's own upload
# timeout (90s in `mobile/lib/api.ts`). Retrying past that would turn a
# recoverable failure into a request the app has already given up on — the
# audio would still be safe, but the user would be told the server was
# unreachable rather than that the words are pending.
_TOTAL_DEADLINE_SECONDS = 75.0

# Sleeping away the budget and then firing a request that cannot finish is
# worse than giving up a moment earlier: the wait buys nothing and the caller
# waits for it anyway. A retry is only taken if this much is left afterwards.
_MIN_ATTEMPT_SECONDS = 2.0

# Transient: worth another attempt. Everything else — 400 bad audio, 401 bad
# key, 413 too large — is settled on the first try, and retrying only makes the
# user wait for the same answer.
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


async def _sleep(seconds: float) -> None:
    """Indirection so tests can assert the backoff without serving it."""
    await asyncio.sleep(seconds)


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    """How long the server asked us to wait, if it said.

    Groq answers a 429 with `retry-after` in seconds. An absurd value is
    ignored rather than honoured: the deadline is the real bound, and a header
    asking for ten minutes is not something a capture can wait out.

    Args:
        response: The refused response.

    Returns:
        Seconds to wait, or None if the header was absent or unusable.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        # The HTTP-date form is legal but nothing here sends it; exponential
        # backoff is a better answer than parsing it wrong.
        return None
    if seconds < 0 or seconds > _TOTAL_DEADLINE_SECONDS:
        return None
    return seconds


class TranscriptionError(Exception):
    """The transcriber failed or returned nothing usable."""


@dataclass(frozen=True)
class Transcript:
    """One transcription, with whatever confidence the transcriber offered."""

    text: str
    confidence: Optional[float]
    source: str  # 'on_device' | 'cloud'

    @property
    def low_confidence(self) -> bool:
        """Below the floor, so the row is worth flagging for review (D13)."""
        if self.confidence is None:
            return False
        return self.confidence < settings.transcript_confidence_floor


def _confidence_from_segments(segments: Any) -> Optional[float]:
    """Collapse Whisper's per-segment log-probabilities into one [0,1] score.

    Whisper reports `avg_logprob` per segment — a mean log-probability per
    token, so `exp` of it is a probability. Segments are weighted by duration
    so a long confident passage is not outvoted by a short uncertain one, and
    `no_speech_prob` discounts segments the model thinks are silence.

    Args:
        segments: The `segments` array from a verbose_json response.

    Returns:
        A score in [0,1], or None if the response carried no usable segments.
    """
    if not isinstance(segments, list) or not segments:
        return None

    weighted = 0.0
    total_weight = 0.0
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        logprob = segment.get("avg_logprob")
        if not isinstance(logprob, (int, float)):
            continue

        start = segment.get("start")
        end = segment.get("end")
        duration = (
            float(end) - float(start)
            if isinstance(start, (int, float)) and isinstance(end, (int, float))
            else 1.0
        )
        weight = max(duration, 0.1)

        probability = math.exp(max(float(logprob), -20.0))
        no_speech = segment.get("no_speech_prob")
        if isinstance(no_speech, (int, float)):
            probability *= 1.0 - min(max(float(no_speech), 0.0), 1.0)

        weighted += probability * weight
        total_weight += weight

    if total_weight == 0.0:
        return None
    return min(max(weighted / total_weight, 0.0), 1.0)


def _decode(payload: Any) -> Transcript:
    """Turn a transcription response into a `Transcript`.

    Args:
        payload: Decoded JSON body from the transcription endpoint.

    Returns:
        The transcript and its confidence.

    Raises:
        TranscriptionError: If the body carries no text.
    """
    if not isinstance(payload, dict):
        raise TranscriptionError("Transcriber returned a non-object body")

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TranscriptionError("Transcriber returned no text")

    return Transcript(
        text=text.strip(),
        # Absent on models that do not support verbose_json; a missing score is
        # not a low score, so it stays None rather than becoming zero.
        confidence=_confidence_from_segments(payload.get("segments")),
        source="cloud",
    )


async def transcribe(
    data: bytes, filename: str = "capture.m4a", content_type: str = "audio/m4a"
) -> Transcript:
    """Transcribe a recording in the cloud (UC8).

    Args:
        data: Raw audio bytes.
        filename: Name to send in the multipart part; the extension is what
            tells the service how to decode the audio.
        content_type: MIME type of the audio.

    Returns:
        The transcript and its confidence.

    Raises:
        TranscriptionError: On any configuration, transport or decoding
            failure, and after a transient failure (429, 5xx, dropped
            connection) has been retried to exhaustion. The caller keeps the
            row and the audio (UC42) — a failed transcription costs the capture
            its words, never the recording.
    """
    if not settings.groq_api_key:
        raise TranscriptionError("GROQ_API_KEY is not configured")
    if not data:
        raise TranscriptionError("Refusing to transcribe an empty recording")

    form: dict[str, str] = {
        "model": settings.groq_model,
        # verbose_json is what carries `segments`, and `segments` is the only
        # confidence signal on offer. Hosts that ignore it return plain json
        # and simply yield a null confidence.
        "response_format": "verbose_json",
    }
    # Pinned to a single language by default (D23). Whisper's own detection is
    # a guess made from the first seconds of audio, and a wrong guess produces
    # confident nonsense rather than an error.
    if settings.groq_language:
        form["language"] = settings.groq_language

    deadline = time.monotonic() + _TOTAL_DEADLINE_SECONDS
    backoff = _BACKOFF_BASE_SECONDS
    last_failure = "the transcriber did not answer"

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        wait: Optional[float] = None
        try:
            # Each attempt is bounded by whatever is left of the overall
            # deadline, so a hung connection cannot spend the budget the
            # retries were supposed to use.
            async with httpx.AsyncClient(
                timeout=min(_TIMEOUT_SECONDS, remaining)
            ) as client:
                response = await client.post(
                    f"{settings.groq_api_base.rstrip('/')}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                    data=form,
                    files={"file": (filename, data, content_type)},
                )
        except httpx.HTTPError as e:
            # A dropped connection is the same transient class as a 429.
            last_failure = f"Could not reach the transcriber: {e}"
        else:
            if response.status_code < 400:
                try:
                    payload = response.json()
                except ValueError as e:
                    raise TranscriptionError(
                        f"Transcriber returned non-JSON: {e}"
                    ) from e
                if attempt > 1:
                    logger.info("Transcription succeeded on attempt %d", attempt)
                return _decode(payload)

            if response.status_code not in _RETRY_STATUSES:
                raise TranscriptionError(
                    f"Transcriber refused ({response.status_code}): "
                    f"{response.text[:200]}"
                )

            last_failure = (
                f"Transcriber refused ({response.status_code}): "
                f"{response.text[:200]}"
            )
            wait = _retry_after_seconds(response)

        if attempt == _MAX_ATTEMPTS:
            break

        if wait is None:
            # Full jitter. Pointless with one user, but it costs nothing and
            # stops a retry storm if this ever runs for more than one.
            wait = random.uniform(0, backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

        remaining = deadline - time.monotonic()
        if wait + _MIN_ATTEMPT_SECONDS > remaining:
            break

        logger.warning(
            "Transcription attempt %d/%d failed (%s); retrying in %.1fs",
            attempt,
            _MAX_ATTEMPTS,
            last_failure,
            wait,
        )
        await _sleep(wait)

    # Out of attempts or out of time. The caller keeps the row and the audio
    # (UC42), so this costs the capture its words, never the capture.
    raise TranscriptionError(last_failure)

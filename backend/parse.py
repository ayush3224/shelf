"""Haiku parse of a raw capture (UC9, UC10, UC12, UC14, UC4).

One small Anthropic call per capture. Classification plus extraction — see
the parse contract in `docs/data-model.md`. Never Sonnet, never Opus, and
never more than `MAX_PARSE_TOKENS` of output (CLAUDE.md cost rules).

A capture holding several things sets `split` (UC4). Only then is there a
second call, and only that one gets a larger budget — the common path stays
one 200-token reply. See D19.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from anthropic import AsyncAnthropic

from backend.config import capture_tz, settings

logger = logging.getLogger(__name__)

KINDS = ("task", "note", "person_note")
ENTITY_TYPES = ("person", "org", "place")

# Kept low on purpose: a capture must not hang on the model, and a retry
# storm is the failure mode that breaks the cost budget.
_TIMEOUT_SECONDS = 20.0
_MAX_RETRIES = 1

SYSTEM_PROMPT = """You extract structured data from one voice capture.

Reply with a single JSON object and nothing else — no markdown, no prose:
{"kind":"task|note|person_note","text":"cleaned one-line description",
"due_at":"ISO-8601 with UTC offset, or null","critical":true|false,
"project_hint":"string or null","entities":[{"type":"person|org|place","name":"..."}],
"split":true|false}

kind: task = something the speaker must do; person_note = about a specific
person; note = anything else.
text: under 80 characters, imperative for a task, no filler words.
due_at: only when the capture actually states a date or time. Resolve
relative expressions against the current time given by the user. If no time
is stated, null — never invent one.
critical: true only on explicit urgency cues ("urgent", "critical",
"don't let me miss this"). A deadline alone is not critical.
project_hint: a project or area name if one is named, else null.
entities: people, orgs and places named in the capture. Empty list if none.
split: true only when the capture holds two or more genuinely separate things
that would each need their own reminder. One thing described at length, or a
task with its context attached, is not a split. When true, still fill every
other field for the first item — the split may not be acted on."""


SPLIT_SYSTEM_PROMPT = """You split one voice capture into its separate items.

Reply with a single JSON object and nothing else — no markdown, no prose:
{"items":[{"kind":"task|note|person_note","text":"cleaned one-line description",
"due_at":"ISO-8601 with UTC offset, or null","critical":true|false,
"project_hint":"string or null","entities":[{"type":"person|org|place","name":"..."}]}]}

Each element follows the same rules as a single parse:
kind: task = something the speaker must do; person_note = about a specific
person; note = anything else.
text: under 80 characters, imperative for a task, no filler words.
due_at: only when that item actually states a date or time. Resolve relative
expressions against the current time given by the user. If no time is stated
for that item, null — never invent one, and never copy another item's time.
critical: true only on explicit urgency cues, per item.
project_hint: a project or area name if one is named, else null.
entities: people, orgs and places named in that item. Empty list if none.

Split only what is genuinely separate. Prefer fewer, larger items over many
fragments: two items is the common case. Never return an empty list — if the
capture turns out to hold one thing, return that one thing."""


class ParseError(Exception):
    """The model call failed or returned something unusable."""


@dataclass
class ParseResult:
    """The parse contract, normalised (UC9, UC10, UC14)."""

    kind: str
    text: str
    due_at: Optional[datetime]
    critical: bool
    project_hint: Optional[str]
    entities: list[dict[str, str]] = field(default_factory=list)
    # UC4: the capture held more than one thing. Advisory — the caller decides
    # whether to spend a second call on it.
    split: bool = False

    @property
    def state(self) -> str:
        """Initial state: timed things are active, untimed things shelve (UC12)."""
        return "active" if self.due_at else "shelved"


def _coerce_due_at(value: Any) -> Optional[datetime]:
    """Turn the model's `due_at` into an aware datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None

    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("Unparseable due_at from model: %r", value)
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=capture_tz())
    return parsed


def _coerce_entities(value: Any) -> list[dict[str, str]]:
    """Keep only well-formed `{type, name}` entities. Linking is UC44, not now."""
    if not isinstance(value, list):
        return []

    entities: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        etype = item.get("type")
        if isinstance(name, str) and name.strip() and etype in ENTITY_TYPES:
            entities.append({"type": etype, "name": name.strip()})
    return entities


def _coerce_text(value: Any, fallback: str) -> str:
    """Cleaned one-line description, falling back to the raw capture."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback.strip()


def _from_dict(data: dict[str, Any], raw_text: str) -> ParseResult:
    """Normalise one parse-contract object into a `ParseResult`.

    Shared by the single parse and by each element of a split array (UC4), so
    the two paths cannot drift in how they coerce a field.

    Args:
        data: One decoded parse-contract object.
        raw_text: The original capture, used as a fallback description.

    Returns:
        The normalised parse.

    Raises:
        ParseError: If `kind` is missing or not one of KINDS.
    """
    kind = data.get("kind")
    if kind not in KINDS:
        raise ParseError(f"Model returned an unknown kind: {kind!r}")

    project_hint = data.get("project_hint")
    if not isinstance(project_hint, str) or not project_hint.strip():
        project_hint = None
    else:
        project_hint = project_hint.strip()

    return ParseResult(
        kind=kind,
        text=_coerce_text(data.get("text"), raw_text),
        due_at=_coerce_due_at(data.get("due_at")),
        critical=bool(data.get("critical")),
        project_hint=project_hint,
        entities=_coerce_entities(data.get("entities")),
        split=bool(data.get("split")),
    )


def _json_body(body: str) -> Any:
    """Strip an accidental code fence and decode the model's reply.

    Args:
        body: Raw text of the model's reply.

    Returns:
        The decoded JSON.

    Raises:
        ParseError: If the reply is not JSON.
    """
    body = body.strip()
    if body.startswith("```"):
        body = body.split("```")[1].removeprefix("json").strip()

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise ParseError(f"Model reply was not JSON: {e}") from e


def _decode(body: str, raw_text: str) -> ParseResult:
    """Validate and normalise the model's JSON reply.

    Args:
        body: Raw text of the model's reply.
        raw_text: The original capture, used as a fallback description.

    Returns:
        The normalised parse.

    Raises:
        ParseError: If the reply is not a JSON object or `kind` is not valid.
    """
    data = _json_body(body)

    if not isinstance(data, dict):
        raise ParseError("Model reply was not a JSON object")

    return _from_dict(data, raw_text)


def _client() -> AsyncAnthropic:
    """Build the Anthropic client.

    Raises:
        ParseError: If no API key is configured.
    """
    if not settings.anthropic_api_key:
        raise ParseError("ANTHROPIC_API_KEY is not configured")
    return AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=_TIMEOUT_SECONDS,
        max_retries=_MAX_RETRIES,
    )


async def parse_capture(raw_text: str, now: Optional[datetime] = None) -> ParseResult:
    """Parse one capture into the parse contract.

    Args:
        raw_text: Transcript or typed input.
        now: Reference time for relative dates; defaults to the current time
            in the configured capture timezone.

    Returns:
        The normalised parse.

    Raises:
        ParseError: On any API or decoding failure. The caller keeps the row
            and flags it (UC42) — a failed parse never loses a capture.
    """
    reference = now or datetime.now(capture_tz())

    try:
        response = await _client().messages.create(
            model=settings.anthropic_model,
            max_tokens=settings.max_parse_tokens,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Current time: {reference.isoformat()}\n"
                        f"Capture: {raw_text}"
                    ),
                }
            ],
        )
    except ParseError:
        raise
    except Exception as e:
        raise ParseError(f"Anthropic call failed: {e}") from e

    if response.stop_reason == "max_tokens":
        raise ParseError("Model reply hit max_tokens and was truncated")

    body = "".join(b.text for b in response.content if b.type == "text")
    if not body:
        raise ParseError(f"Model returned no text (stop_reason={response.stop_reason})")

    return _decode(body, raw_text)


def _decode_split(body: str, raw_text: str) -> list[ParseResult]:
    """Validate and normalise a split reply (UC4).

    Args:
        body: Raw text of the model's reply.
        raw_text: The original capture, used as a fallback description.

    Returns:
        One `ParseResult` per item, capped at `MAX_SPLIT_ITEMS`.

    Raises:
        ParseError: If the reply carries no usable items. The caller falls back
            to the single parse it already has rather than losing the capture.
    """
    data = _json_body(body)
    if not isinstance(data, dict):
        raise ParseError("Split reply was not a JSON object")

    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ParseError("Split reply carried no items")

    results: list[ParseResult] = []
    for element in items[: settings.max_split_items]:
        if not isinstance(element, dict):
            continue
        try:
            results.append(_from_dict(element, raw_text))
        except ParseError as e:
            # One malformed element does not sink the rest. Losing an item in a
            # split is bad; losing all of them because of one is worse.
            logger.warning("Dropping a malformed split item: %s", e)

    if not results:
        raise ParseError("No split item survived normalisation")

    if len(items) > settings.max_split_items:
        logger.warning(
            "Split returned %d items; kept the first %d",
            len(items),
            settings.max_split_items,
        )

    return results


async def parse_split(
    raw_text: str, now: Optional[datetime] = None
) -> list[ParseResult]:
    """Re-prompt for the separate items in a multi-item capture (UC4).

    Only called when a first parse set `split`. That keeps the common path at
    one call of at most `MAX_PARSE_TOKENS`; this second call gets
    `MAX_SPLIT_TOKENS` because an array cannot fit in 200 (D19).

    Args:
        raw_text: The same transcript the first parse saw.
        now: Reference time for relative dates; defaults to the current time
            in the configured capture timezone.

    Returns:
        One `ParseResult` per item, in the order the capture stated them.

    Raises:
        ParseError: On any API or decoding failure. The caller keeps the single
            parse it already has — a failed split degrades to one item, it
            never loses the capture (UC42).
    """
    reference = now or datetime.now(capture_tz())

    try:
        response = await _client().messages.create(
            model=settings.anthropic_model,
            max_tokens=settings.max_split_tokens,
            system=SPLIT_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Current time: {reference.isoformat()}\nCapture: {raw_text}"
                    ),
                }
            ],
        )
    except ParseError:
        raise
    except Exception as e:
        raise ParseError(f"Anthropic split call failed: {e}") from e

    if response.stop_reason == "max_tokens":
        # Truncated JSON is unrecoverable, and raising here is what makes the
        # caller fall back to the single item rather than write half a split.
        raise ParseError("Split reply hit max_tokens and was truncated")

    body = "".join(b.text for b in response.content if b.type == "text")
    if not body:
        raise ParseError(f"Split returned no text (stop_reason={response.stop_reason})")

    return _decode_split(body, raw_text)

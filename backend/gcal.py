"""Google Calendar writes for timed items (UC43).

One-way, always: the app owns the item and the event is a projection of it
(D8). Nothing here ever reads a state back out of Google — not the summary,
not the time, not whether the event still exists. If somebody deletes an event
by hand in Google Calendar, the next sync puts it back, because the item is
what is true and the calendar is a copy.

The credential is a **service account** with the owner's personal calendar
shared to it as a writer, not the OAuth user flow the architecture doc
originally sketched (D52). That removes the refresh-token dance entirely:
there is no consent screen, no token to store per user and no expiry to nurse
— just a key file and a calendar id.

Nothing here decides anything either. It is handed a desired event and told
which calendar; the scheduler decides what should exist, in the same way it
decides what to push. The one judgement this module does make is which
failures are worth trying again, because a caller that cannot tell "Google was
briefly unreachable" from "that event is gone" will either give up on a
working sync or retry a 404 forever.
"""

import asyncio
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
import jwt

from backend.config import capture_tz, settings

logger = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/calendar/v3"

#: Least privilege. Events CRUD is the whole of UC43; the wider `calendar`
#: scope would also hand out the calendar's settings and ACLs, and this key
#: sits in a file on a VPS.
SCOPE = "https://www.googleapis.com/auth/calendar.events"

#: Google's access tokens last an hour. Renew early rather than on expiry, so
#: a token that dies mid-request is a retry rather than a failed sync.
_TOKEN_SKEW_SECONDS = 300

#: Same reasoning as the push service: the tick comes round in a minute, so
#: hanging it on a slow POST is worse than trying again shortly.
_TIMEOUT_SECONDS = 20.0

#: The key under `extendedProperties.private` that ties an event back to the
#: item it came from. Private to the app that wrote it, invisible in the UI,
#: and the only thing that makes a stray event identifiable months later.
ITEM_ID_PROPERTY = "shelf_item_id"


class CalendarError(Exception):
    """A calendar write did not happen.

    Args:
        message: What went wrong.
        retryable: Whether trying again later could plausibly work. False
            means the request itself is wrong — a bad key, a calendar the
            service account cannot write to — and burning attempts on it only
            delays somebody reading the log.
        gone: The event this was about does not exist on Google's side.
    """

    def __init__(
        self, message: str, *, retryable: bool = True, gone: bool = False
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.gone = gone


@dataclass(frozen=True)
class CalendarEvent:
    """The event one item projects onto the calendar.

    Args:
        item_id: The item this is a projection of.
        text: What the item says — the event's summary.
        due_at: When it is due. Timezone-aware; the calendar shows it in the
            capture timezone.
        raw_text: The original transcript, carried into the description only
            when it differs from `text` (D14: the edit lands on `parsed_text`,
            and what was actually said is worth having next to it).
    """

    item_id: str
    text: str
    due_at: Any
    raw_text: str = ""

    def body(self) -> dict[str, Any]:
        """The event as Google's API wants it.

        Two choices in here are deliberate and neither is Google's default:

        - **No reminders.** The app is the reminder system (UC23). Leaving
          Google's defaults on means every timed item notifies twice, from two
          apps, at two slightly different moments — which is how a person
          learns to ignore both.
        - **Transparent, not busy.** These are reminders, not meetings. A
          fifteen-minute block that makes the owner look unavailable would
          make the calendar worse at the one job it already had.

        Returns:
            A Calendar API event resource.
        """
        start = self.due_at.astimezone(capture_tz())
        end = start + timedelta(minutes=settings.google_calendar_event_minutes)
        zone = settings.capture_timezone

        description = f"Shelf item {self.item_id}"
        stripped = (self.raw_text or "").strip()
        if stripped and stripped != self.text.strip():
            description = f"{stripped}\n\n{description}"

        return {
            # Google shows an empty summary as "(No title)". A capture that
            # produced no usable text is already flagged for review (UC42);
            # the calendar should say something findable rather than nothing.
            "summary": (self.text.strip() or "Untitled Shelf item")[:1024],
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": zone},
            "end": {"dateTime": end.isoformat(), "timeZone": zone},
            "reminders": {"useDefault": False, "overrides": []},
            "transparency": "transparent",
            "source": {"title": "Shelf", "url": "https://shelf.local"},
            "extendedProperties": {"private": {ITEM_ID_PROPERTY: self.item_id}},
        }


# ------------------------------------------------------------------- auth

_token: Optional[str] = None
_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()


def enabled() -> bool:
    """Whether the calendar sync is configured at all.

    A missing calendar id is not an error: this is a P1 integration on a
    single-user app, and a deployment without it should tick quietly rather
    than log a failure every minute forever.

    Returns:
        True if there is a calendar to write to and a key to write with.
    """
    return bool(settings.google_calendar_id) and bool(settings.google_calendar_key_file)


def _service_account() -> dict[str, str]:
    """Read the service account key file.

    Returns:
        The parsed key.

    Raises:
        CalendarError: If the file is missing or not a service account key.
            Not retryable — no amount of waiting fixes a path.
    """
    path = Path(settings.google_calendar_key_file)
    try:
        key = json.loads(path.read_text())
    except FileNotFoundError as e:
        raise CalendarError(
            f"No Google service account key at {path}", retryable=False
        ) from e
    except (OSError, ValueError) as e:
        raise CalendarError(
            f"Could not read the Google service account key: {e}", retryable=False
        ) from e

    missing = [
        field
        for field in ("client_email", "private_key", "token_uri")
        if not key.get(field)
    ]
    if missing:
        raise CalendarError(
            f"Service account key is missing {', '.join(missing)}", retryable=False
        )
    return key


async def access_token() -> str:
    """A bearer token for the Calendar API, minted from the service account.

    The service account signs a short-lived assertion about itself and trades
    it for an access token — the two-legged flow, with no user in it. Tokens
    are cached until shortly before they expire, which for the one-minute tick
    means roughly one token exchange an hour rather than one a minute.

    Returns:
        A bearer token.

    Raises:
        CalendarError: If the key is unusable, or Google refused the exchange.
    """
    global _token, _token_expires_at

    async with _token_lock:
        if _token and time.time() < _token_expires_at:
            return _token

        key = _service_account()
        now = int(time.time())
        try:
            assertion = jwt.encode(
                {
                    "iss": key["client_email"],
                    "scope": SCOPE,
                    "aud": key["token_uri"],
                    "iat": now,
                    "exp": now + 3600,
                },
                key["private_key"],
                algorithm="RS256",
                headers={"kid": key.get("private_key_id", "")},
            )
        except Exception as e:  # a malformed private key, in practice
            raise CalendarError(
                f"Could not sign the service account assertion: {e}", retryable=False
            ) from e

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    key["token_uri"],
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                )
        except httpx.HTTPError as e:
            raise CalendarError(f"Could not reach Google for a token: {e}") from e

        if response.status_code >= 400:
            # A refused *assertion* is a bad key or a disabled account, not a
            # blip: retrying it every minute would hide the real problem.
            raise CalendarError(
                f"Google refused the token request ({response.status_code}): "
                f"{response.text[:300]}",
                retryable=response.status_code >= 500,
            )

        try:
            payload = response.json()
        except ValueError as e:
            raise CalendarError("Google's token response was not JSON") from e

        token = payload.get("access_token")
        if not token:
            raise CalendarError("Google's token response carried no access token")

        expires_in = int(payload.get("expires_in") or 3600)
        _token = token
        _token_expires_at = time.time() + max(0, expires_in - _TOKEN_SKEW_SECONDS)
        return token


def forget_token() -> None:
    """Drop the cached access token.

    Called after a 401. The token has an hour on it and is renewed early, so a
    401 means the cached one is bad rather than old — keeping it would fail
    every remaining item in the same tick for the same stale reason.
    """
    global _token, _token_expires_at
    _token = None
    _token_expires_at = 0.0


# ------------------------------------------------------------- the calls


def _events_url(calendar_id: str, event_id: str = "") -> str:
    """The events endpoint for a calendar, or for one event on it.

    Args:
        calendar_id: The calendar. Usually an email address, which has to be
            escaped or the `@` ends the path segment early.
        event_id: An event on it, or empty for the collection.

    Returns:
        An absolute URL.
    """
    quoted = urllib.parse.quote(calendar_id, safe="")
    url = f"{API_BASE}/calendars/{quoted}/events"
    return f"{url}/{urllib.parse.quote(event_id, safe='')}" if event_id else url


def _classify(response: httpx.Response, what: str) -> CalendarError:
    """Turn a failed response into an error the caller can act on.

    Args:
        response: What Google said.
        what: The operation, for the message.

    Returns:
        The error to raise.
    """
    status = response.status_code
    detail = response.text[:300]

    if status in (404, 410):
        return CalendarError(f"{what}: the event no longer exists", gone=True)
    if status == 401:
        # The token, not the permissions. Worth another go with a fresh one.
        return CalendarError(f"{what}: Google rejected the token ({detail})")
    if status == 403:
        # Sharing removed, quota exhausted, or the API turned off. Google
        # overloads 403 for rate limiting, which *is* worth retrying.
        rate_limited = "rateLimitExceeded" in detail or "userRateLimit" in detail
        return CalendarError(
            f"{what}: Google refused the write ({detail})", retryable=rate_limited
        )
    if status == 429 or status >= 500:
        return CalendarError(f"{what}: Google returned {status} ({detail})")
    return CalendarError(
        f"{what}: Google returned {status} ({detail})", retryable=False
    )


async def _request(
    method: str, url: str, *, json_body: Optional[dict[str, Any]], what: str
) -> Optional[dict[str, Any]]:
    """One Calendar API call, with a single retry on a stale token.

    Args:
        method: HTTP method.
        url: Absolute URL.
        json_body: Body to send, or None.
        what: The operation, for error messages.

    Returns:
        The parsed response, or None when Google sent no body.

    Raises:
        CalendarError: On anything other than success.
    """
    for attempt in (1, 2):
        token = await access_token()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.request(
                    method,
                    url,
                    headers={
                        "authorization": f"Bearer {token}",
                        "content-type": "application/json",
                    },
                    json=json_body,
                )
        except httpx.HTTPError as e:
            raise CalendarError(f"{what}: could not reach Google ({e})") from e

        if response.status_code == 401 and attempt == 1:
            forget_token()
            continue

        if response.status_code >= 400:
            raise _classify(response, what)

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    raise CalendarError(f"{what}: Google kept rejecting the token")


async def create_event(calendar_id: str, event: CalendarEvent) -> str:
    """Put a new event on the calendar.

    Args:
        calendar_id: Calendar to write to.
        event: What the item projects.

    Returns:
        Google's event id, to be stored against the item.

    Raises:
        CalendarError: If the write did not happen.
    """
    body = await _request(
        "POST",
        _events_url(calendar_id),
        json_body=event.body(),
        what=f"create event for item {event.item_id}",
    )
    event_id = (body or {}).get("id")
    if not event_id:
        raise CalendarError(
            f"create event for item {event.item_id}: Google returned no event id"
        )
    return str(event_id)


async def patch_event(calendar_id: str, event_id: str, event: CalendarEvent) -> None:
    """Bring an existing event back in line with its item.

    A PATCH rather than a PUT: the fields sent here are the ones the app owns,
    and replacing the whole resource would also clobber anything the owner
    added on Google's side — a location typed on a phone, a guest invited.
    Those are not the app's to erase.

    Args:
        calendar_id: Calendar the event is on.
        event_id: Google's id for it.
        event: What the item now says.

    Raises:
        CalendarError: If the write did not happen. `gone` is set if the event
            has been deleted on Google's side.
    """
    await _request(
        "PATCH",
        _events_url(calendar_id, event_id),
        json_body=event.body(),
        what=f"update event for item {event.item_id}",
    )


async def delete_event(calendar_id: str, event_id: str) -> None:
    """Remove an event from the calendar.

    An event that is already gone is a success, not a failure: the caller
    wanted it absent and it is absent. Treating that as an error would leave
    completed items retrying a delete forever against a calendar that already
    agrees.

    Args:
        calendar_id: Calendar the event is on.
        event_id: Google's id for it.

    Raises:
        CalendarError: If the delete did not happen for any other reason.
    """
    try:
        await _request(
            "DELETE",
            _events_url(calendar_id, event_id),
            json_body=None,
            what=f"delete event {event_id}",
        )
    except CalendarError as e:
        if e.gone:
            return
        raise

"""Push delivery through Expo's push service (UC23).

The app is Expo, so the token is an `ExponentPushToken[...]` rather than a raw
FCM registration id, and Expo's service is what stands in front of FCM. That
is the whole reason this module is not an FCM client: the credential Google
issued lives in Expo's dashboard (the FCM V1 service account key uploaded to
EAS), and talking to FCM directly would mean managing that key twice.

Nothing here decides anything. It sends what the scheduler hands it and
reports back, per token, what happened — because the one thing the scheduler
must never do is treat a push that failed to leave as a push the user ignored.
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

# A push is not on anyone's critical path — the tick comes round again in a
# minute — so this is short. Hanging the whole tick on a slow POST is worse
# than trying again shortly.
_TIMEOUT_SECONDS = 20.0

# Expo accepts up to 100 messages per request. The scheduler's own batch limit
# is well under this; the constant is here so the two cannot silently disagree.
MAX_MESSAGES_PER_REQUEST = 100

# Expo's word for "this token belongs to an app that is no longer installed,
# or the install was replaced". It is the one error that means *stop using
# this token* rather than *try again later*.
DEVICE_NOT_REGISTERED = "DeviceNotRegistered"


class PushError(Exception):
    """The push service could not be reached, or refused the whole request."""


@dataclass(frozen=True)
class PushMessage:
    """One notification, addressed to one device."""

    token: str
    title: str
    body: str
    data: dict[str, str]

    def payload(self) -> dict[str, Any]:
        """The wire form Expo expects.

        `categoryId` is what puts the Done and Snooze buttons on the
        notification (UC15, UC17) — the category itself is registered by the
        app, so this is a name both sides have to agree on, and it comes from
        config for that reason. `channelId` is the Android channel, likewise.

        Returns:
            A single Expo push message.
        """
        return {
            "to": self.token,
            "title": self.title,
            "body": self.body,
            "data": self.data,
            "sound": "default",
            # The item is due *now*; a batched delivery would arrive after the
            # moment it is about.
            "priority": "high",
            "channelId": settings.push_channel_id,
            "categoryId": settings.push_category_id,
        }


@dataclass(frozen=True)
class PushTicket:
    """What Expo said about one message.

    Args:
        token: The device this ticket is about.
        ok: Whether Expo accepted the message for delivery.
        ticket_id: Expo's receipt id, when it accepted.
        error: Expo's error code, when it did not.
        message: The human-readable reason, when it did not.
    """

    token: str
    ok: bool
    ticket_id: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None

    @property
    def token_is_dead(self) -> bool:
        """Whether this token should stop being used at all."""
        return self.error == DEVICE_NOT_REGISTERED


def is_expo_token(token: str) -> bool:
    """Whether a string looks like an Expo push token.

    Cheap validation at the edge. A malformed token stored now is a push that
    silently goes nowhere later, and the device is not around to ask again.

    Args:
        token: Candidate token.

    Returns:
        True if it has the shape Expo issues.
    """
    token = token.strip()
    return (
        token.startswith(("ExponentPushToken[", "ExpoPushToken["))
        and token.endswith("]")
        and len(token) > len("ExponentPushToken[]")
    )


def _headers() -> dict[str, str]:
    """Request headers, with the access token only if one is configured."""
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
    }
    if settings.expo_access_token:
        headers["authorization"] = f"Bearer {settings.expo_access_token}"
    return headers


def _tickets_from(data: list[Any], messages: list[PushMessage]) -> list[PushTicket]:
    """Pair Expo's ticket array back up with the messages that produced it.

    Expo answers positionally and does not echo the token, so the pairing is
    by index. A response of the wrong length is treated as unusable rather
    than guessed at — mis-attributing a `DeviceNotRegistered` would disable a
    working device.

    Args:
        data: The `data` array from Expo's response.
        messages: The messages sent, in the order they were sent.

    Returns:
        One ticket per message.

    Raises:
        PushError: If the response does not line up with the request.
    """
    if len(data) != len(messages):
        raise PushError(
            f"Expo returned {len(data)} tickets for {len(messages)} messages"
        )

    tickets: list[PushTicket] = []
    for message, entry in zip(messages, data):
        if not isinstance(entry, dict):
            raise PushError(f"Expo returned a non-object ticket: {entry!r}")

        if entry.get("status") == "ok":
            tickets.append(
                PushTicket(token=message.token, ok=True, ticket_id=entry.get("id"))
            )
            continue

        details = entry.get("details")
        error = details.get("error") if isinstance(details, dict) else None
        tickets.append(
            PushTicket(
                token=message.token,
                ok=False,
                error=error,
                message=str(entry.get("message") or "Expo refused the message"),
            )
        )
    return tickets


async def send(messages: list[PushMessage]) -> list[PushTicket]:
    """Hand a batch of notifications to Expo.

    Args:
        messages: What to send. At most `MAX_MESSAGES_PER_REQUEST`.

    Returns:
        One ticket per message, in the order given.

    Raises:
        PushError: If the request failed as a whole — a refused request tells
            you nothing about any individual message, and the caller must be
            able to tell that apart from "Expo rejected this token".
    """
    if not messages:
        return []
    if len(messages) > MAX_MESSAGES_PER_REQUEST:
        raise PushError(
            f"{len(messages)} messages exceeds Expo's limit of "
            f"{MAX_MESSAGES_PER_REQUEST} per request"
        )

    body = [message.payload() for message in messages]

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                settings.expo_push_url, headers=_headers(), json=body
            )
    except httpx.HTTPError as e:
        raise PushError(f"Could not reach the push service: {e}") from e

    if response.status_code >= 400:
        raise PushError(
            f"Push service returned {response.status_code}: {response.text[:300]}"
        )

    try:
        parsed = response.json()
    except ValueError as e:
        raise PushError("Push service returned a body that is not JSON") from e

    if not isinstance(parsed, dict):
        raise PushError(f"Push service returned {type(parsed).__name__}, not an object")

    # A request-level error — a bad access token, a malformed body — comes back
    # here instead of in `data`, and applies to every message.
    errors = parsed.get("errors")
    if errors:
        raise PushError(f"Push service refused the request: {errors}")

    data = parsed.get("data")
    if not isinstance(data, list):
        raise PushError("Push service returned no ticket array")

    return _tickets_from(data, messages)

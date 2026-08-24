"""Configuration from environment variables."""

import logging
from datetime import timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """App settings from .env file."""

    database_url: str
    db_schema: str = "shelf"
    api_port: int = 8001
    debug: bool = False

    supabase_jwt_secret: str = ""
    supabase_jwt_aud: str = "authenticated"

    shelve_after_ignores: int = 3
    drop_after_days: int = 90
    quiet_hours_start: int = 22
    quiet_hours_end: int = 7
    max_parse_tokens: int = 200

    # Timezone relative dates in a capture ("tomorrow 3pm") resolve against.
    # `TZ` is the name systemd already exports to the process; CAPTURE_TIMEZONE
    # is kept as an alias so an existing .env keeps working.
    capture_timezone: str = Field(
        default="UTC",
        validation_alias=AliasChoices("TZ", "CAPTURE_TIMEZONE"),
    )

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"

    # A split re-prompt (UC4) returns an array, so it cannot live inside the
    # 200-token cap the common path is held to. See D19.
    max_split_tokens: int = 600
    max_split_items: int = 10

    # Supabase Storage holds the raw audio (UC7). The service key is used, not
    # the anon key: the API is the only writer and RLS does not apply to it.
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_storage_bucket: str = "shelf-audio"
    audio_url_ttl_seconds: int = 3600

    # Cloud transcription, on Groq (D24). The endpoint is OpenAI-compatible
    # `/audio/transcriptions`, so pointing this at another host is a base-URL
    # change and nothing else.
    groq_api_base: str = "https://api.groq.com/openai/v1"
    groq_api_key: str = ""
    # Turbo: the free tier covers this volume outright, so the choice is
    # latency rather than cost, and turbo is the faster of the two (D24).
    groq_model: str = "whisper-large-v3-turbo"
    # Pinned to English (D23). The user speaks only English, so telling the
    # model that removes its language-detection guess as a failure mode.
    groq_language: str = "en"
    # Below this, the transcript is kept but the row is flagged needs_review.
    # Calibrated against real recordings, which land 0.70-0.76 even when the
    # transcript is word-perfect — a phone mic in a room is not the clean
    # synthetic audio the first value was set from (D27).
    transcript_confidence_floor: float = 0.5

    # Push delivery (UC23), via Expo's push service in front of FCM. The
    # access token is only needed if push security is enabled on the Expo
    # account; without it the endpoint accepts unauthenticated sends.
    expo_push_url: str = "https://exp.host/--/api/v2/push/send"
    expo_access_token: str = ""
    # Android notification channel and the category carrying the done/snooze
    # buttons. Both are created by the app; the server names them in every
    # message, so the two sides have to agree and these are that agreement.
    push_channel_id: str = "reminders"
    push_category_id: str = "shelf.reminder"

    # How long an unanswered push waits before the next one comes due — and
    # with it, the write that reads the silence as `ignored` (D32). Three of
    # these is a shelving, so this constant sets how fast decay actually runs.
    # Four hours, not one: an hour made three ignores fit inside a single long
    # meeting, which is an interruption rather than avoidance (D40).
    push_repeat_minutes: int = 240
    # Default snooze (UC17). The client may ask for another value; anything
    # past the ceiling is refused rather than quietly clamped.
    snooze_minutes: int = 30
    max_snooze_minutes: int = 60 * 24 * 7

    # How many notifications one tick will try to send. A cap so that a first
    # run against a backlog cannot turn into a hundred pushes at once.
    push_batch_limit: int = 20
    # After this many failed attempts the row stalls instead of being retried
    # forever. It is never marked sent, so a broken delivery path cannot decay
    # anything (D32).
    push_max_attempts: int = 5

    # The weekly digest (UC31) — the only surface on which silent decay is
    # visible at all, since UC22 was dropped. Named by day rather than by
    # weekday index because that is what `docs/data-model.md` documents and
    # what somebody editing `.env` would expect to write.
    digest_day: str = "sunday"
    # Local hour on that day, in `capture_timezone`. Morning, because the
    # digest is meant to be read before the week starts rather than filed.
    digest_hour: int = 9
    # How far ahead the digest warns that something is about to drop. Two
    # digest cycles, not one: at seven days an item gets exactly one warning
    # and a week you did not read it is a week it dropped unannounced.
    digest_warn_days: int = 14
    # A digest is a snapshot of a week. If it could not be delivered within
    # this long of the week ending, it is abandoned rather than sent late —
    # unlike an item reminder, which is still true whenever it arrives.
    digest_max_age_hours: int = 24
    digest_max_attempts: int = 5
    # How many rows each section of the digest returns. The screen is meant to
    # be read, not scrolled; past this it says how many more there were.
    digest_list_limit: int = 20
    # The digest gets its own Android channel. The reminder channel is HIGH
    # importance because a due item is a moment that has arrived; a weekly
    # summary is not, and giving it the same interruption is how the important
    # channel gets muted.
    digest_channel_id: str = "digest"

    # Google Calendar (UC43). One-way: timed items are projected onto the
    # owner's calendar and nothing is ever read back (D8).
    #
    # A service account with the calendar shared to it, not the OAuth user
    # flow (D52) — so the credential is a file on disk and a calendar id,
    # with no refresh token to store and nothing to re-consent to.
    google_calendar_key_file: str = "google-calendar-key.json"
    # The calendar to write to, normally the owner's own address. Empty turns
    # the whole sync off, which is the right default for a P1 integration: a
    # deployment without it should tick quietly, not fail every minute.
    google_calendar_id: str = ""
    # An item is a moment, not a meeting. Fifteen minutes is long enough to
    # read on a week view and short enough not to look like a commitment —
    # and the events are transparent anyway, so they never mark you busy.
    google_calendar_event_minutes: int = 15
    # How many links one tick will reconcile. Same reasoning as the push
    # batch: a first run against a backfill must not become a hundred writes
    # to Google at once.
    google_calendar_batch_limit: int = 20
    # After this many failed syncs the row stops being retried. It is reset
    # to zero whenever the item is touched again, so giving up is never
    # permanent — a Google outage costs a stalled row, not a lost event.
    google_calendar_max_attempts: int = 5

    model_config = {
        "env_file": ".env",
        "case_sensitive": False,
        "extra": "allow",
    }


settings = Settings()  # type: ignore


#: `datetime.weekday()` indices, so a config typo is caught here rather than
#: producing a digest on a day nobody expected.
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def digest_weekday() -> int:
    """The weekday the digest lands on, as `datetime.weekday()` numbers it.

    Falls back to Sunday on an unrecognised name, and says so. A digest on the
    wrong day is a nuisance; a digest that never fires because the config did
    not parse is the failure this project cannot see, because the whole feature
    is the absence of noise.

    Returns:
        0 for Monday through 6 for Sunday.
    """
    try:
        return _WEEKDAYS[settings.digest_day.strip().lower()]
    except KeyError:
        logger.warning("Unknown DIGEST_DAY %r; using sunday", settings.digest_day)
        return _WEEKDAYS["sunday"]


def capture_tz() -> tzinfo:
    """The timezone the user captures in, falling back to UTC (D15).

    The server runs in UTC and the user does not, so anything resolving a
    wall-clock notion — relative dates in the parse, "end of today" on the
    `Today` list — goes through this rather than the server clock.

    Returns:
        The configured timezone, or UTC if the name is unknown.
    """
    try:
        return ZoneInfo(settings.capture_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "Unknown CAPTURE_TIMEZONE %r; using UTC", settings.capture_timezone
        )
        return timezone.utc

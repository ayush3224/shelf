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

    model_config = {
        "env_file": ".env",
        "case_sensitive": False,
        "extra": "allow",
    }


settings = Settings()  # type: ignore


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

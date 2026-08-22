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

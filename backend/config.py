"""Configuration from environment variables."""

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


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

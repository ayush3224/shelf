"""Configuration from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """App settings from .env file."""

    database_url: str
    db_schema: str = "shelf"
    api_port: int = 8001
    debug: bool = False
    default_user_id: str

    shelve_after_ignores: int = 3
    drop_after_days: int = 90
    quiet_hours_start: int = 22
    quiet_hours_end: int = 7

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"

    model_config = {
        "env_file": ".env",
        "case_sensitive": False,
        "extra": "allow",
    }


settings = Settings()  # type: ignore

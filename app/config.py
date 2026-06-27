"""Application configuration loaded from environment / .env via pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Values are read from environment variables, falling back to a local
    ``.env`` file when present. See ``.env.example`` for the available keys.
    """

    app_name: str = "Feature Flag API"
    log_level: str = "INFO"
    cache_ttl_seconds: int = 300
    host: str = "0.0.0.0"
    port: int = 8000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

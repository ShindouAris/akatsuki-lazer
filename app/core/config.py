"""Application configuration using pydantic-settings."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Server
    app_name: str = "py-lazer-server"
    debug: bool = False
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    # Database
    database_url: str = "sqlite+aiosqlite:///./osu.db"

    # Redis (for caching and pub/sub)
    redis_url: str = "redis://localhost:6379/0"

    # OAuth2 / JWT
    secret_key: str = "change-me-in-production-use-openssl-rand-hex-32"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days
    refresh_token_expire_days: int = 30

    # OAuth2 Client (for lazer client)
    oauth_client_id: str = "5"
    oauth_client_secret: str = "change-me"

    # External URLs
    api_base_url: str = "http://localhost:8000"
    website_url: str = "http://localhost:8000"

    # File storage
    beatmaps_path: str = "./data/beatmaps"
    replays_path: str = "./data/replays"
    avatars_path: str = "./data/avatars"

    # Rate limiting
    rate_limit_requests: int = 1200
    rate_limit_window_seconds: int = 600  # 10 minutes

    # Server mode
    server_mode: Literal["development", "production"] = "development"

    # Beatmap mirror settings
    # When True, uses internal mirror (beatmaps.akatsuki.gg) - requires IP whitelist
    # When False, uses official osu! API directly (requires osu_api credentials)
    use_beatmap_mirror: bool = True
    beatmap_mirror_url: str = "https://beatmaps.akatsuki.gg"

    # Official osu! API v2 credentials (for direct API access when not using mirror)
    osu_api_client_id: str = ""
    osu_api_client_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()

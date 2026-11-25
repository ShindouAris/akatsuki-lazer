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
    # SSL=true serves HTTPS with the two PEM files below (development CA / self-signed certs);
    # SSL=false serves plain HTTP, for when nginx + certbot or a Cloudflare Tunnel terminates TLS.
    ssl: bool = False
    ssl_keyfile: str = "certs/localhost-key.pem"
    ssl_certfile: str = "certs/localhost.pem"

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

    # Object storage for replays and beatmap files (see app/services/storage.py). Backends are tried in this order:
    # S3 (when s3_bucket is set), SeaweedFS (when seaweedfs_endpoint_url is set), then the local paths above.
    s3_bucket: str = ""
    s3_endpoint_url: str = ""  # empty = AWS; set it for another S3-compatible service
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    seaweedfs_endpoint_url: str = ""  # the S3 gateway, e.g. http://seaweedfs:8333
    seaweedfs_bucket: str = "akatsuki"
    seaweedfs_access_key: str = ""
    seaweedfs_secret_key: str = ""
    storage_timeout_seconds: float = 5.0  # connect timeout; a backend that does not answer is skipped

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

    # PP calculation settings
    pp_validate_client_value: bool = True
    pp_tolerance: float = 0.1
    pp_use_server_value_on_mismatch: bool = False
    # Relax / Autopilot plays are worth what the same play is without the mod (Akatsuki style) instead of osu!'s
    # reduced formula, and the server's value always replaces the client's on those boards.
    pp_relax_autopilot_unnerfed: bool = True
    # A play worth more pp than its board's cap is kept, but awards no pp (score-service-rs `performance_caps`).
    # 0 means no cap.
    pp_cap_vanilla: float = 0.0
    pp_cap_relax: float = 0.0
    pp_cap_autopilot: float = 0.0

    # Internal API shared with sibling services such as the Go multiplayer hub.
    # Empty disables /internal/* entirely; set a long random value to enable it.
    internal_api_key: str = ""

    # Set when the realtime multiplayer hub runs as a separate service (see ./multiplayer):
    # this process then stops serving the /multiplayer SignalR endpoint.
    use_external_multiplayer: bool = False

    # Same for the spectator hub (./multiplayer/cmd/spectator). The URL is the service's plain-HTTP internal
    # address, used to push "score processed" notifications to it.
    use_external_spectator: bool = False
    spectator_internal_url: str = "http://spectator:8083"

    # Same for the metadata hub (./multiplayer/cmd/metadata): presence, daily challenge, beatmap updates.
    use_external_metadata: bool = False
    metadata_internal_url: str = "http://metadata:8085"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()

"""Central configuration, read from environment variables / backend/.env."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Storage
    # Local Docker (TimescaleDB image) by default. For Supabase, paste the "Session pooler"
    # connection string from the project's Connect dialog (see .env.example).
    database_url: str = "postgresql://postgres:drivesense@localhost:5433/drivesense"
    database_disable_prepared: bool = False      # forced on automatically for port 6543 (transaction pooler)
    database_pool_max: int = 10                  # keep below the Supabase pooler's per-project limit
    use_timescaledb: str = "auto"                # auto | false  (set false on Supabase)
    # "memory://" runs the hot layer (latest state, live push, commands, leaderboard) inside
    # the API process instead of Redis - fine for one car and one API process.
    redis_url: str = "memory://"
    telemetry_retention_days: int = 90

    # Generative AI (Gemini free tier). Leave the key empty to run in offline mode.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_embed_model: str = "gemini-embedding-001"
    embed_dim: int = 768

    # Shared secret the (fake) vehicle devices send in the X-Device-Key header.
    device_key: str = "dev-device-key"

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()

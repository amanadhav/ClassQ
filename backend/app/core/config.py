"""Application configuration.

Environment variables are loaded via pydantic-settings. Defaults point at the
local Docker Compose stack (see infrastructure/docker-compose.yml):
  - PostgreSQL: localhost:5432, db/user/password = classq
  - Redis:      localhost:6379
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLASSQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- PostgreSQL ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "classq"
    postgres_user: str = "classq"
    postgres_password: str = "classq"
    postgres_pool_min_size: int = 1
    postgres_pool_max_size: int = 10

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    # --- Rate limiting (R3 defaults: 60s window, 100 requests per student) ---
    rate_limit_window_ms: int = 60_000
    rate_limit_student_limit: int = 100

    # --- Seat allocation / waitlist ---
    seat_lock_ttl_seconds: int = 15  # R2.4 default lock duration
    max_waitlist_capacity: int = 50  # R12 Maximum_Waitlist_Capacity default

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


settings = Settings()

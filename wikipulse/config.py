"""All WikiPulse settings, in one place.

Values come from environment variables first, then from a `.env` file in the working
directory, then from the defaults below. Field names map to environment variables
case-insensitively, so `redis_url` is set by `REDIS_URL`.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Text that marks a User-Agent the human has not filled in yet.
USER_AGENT_PLACEHOLDER_MARKERS: tuple[str, ...] = ("<you>", "<your email>", "example.com")

DEFAULT_USER_AGENT = "WikiPulse/0.1 (https://github.com/<you>/wikipulse; <your email>)"


class Settings(BaseSettings):
    """Application settings loaded from the environment and `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env also holds Docker Compose variables we don't use here
    )

    redis_url: str = "redis://localhost:6379/0"
    postgres_dsn: str = "postgresql://wikipulse:wikipulse@localhost:5432/wikipulse"
    # Deliberately not a working value: Wikimedia requires a real contact address.
    user_agent: str = DEFAULT_USER_AGENT
    log_level: LogLevel = "INFO"
    data_dir: Path = Path("data")

    # --- Stage 1: live stream consumer ---
    # Wikimedia EventStreams endpoint for all recent changes (Server-Sent Events).
    stream_url: str = "https://stream.wikimedia.org/v2/stream/recentchange"
    # Redis Stream that receives the filtered, normalized edits.
    stream_key: str = "wiki:edits"
    # Approximate upper bound on the Redis Stream length (XADD MAXLEN ~).
    stream_maxlen: int = Field(default=1_000_000, gt=0)
    # Longest wait between reconnect attempts, in seconds.
    reconnect_max_s: float = Field(default=60.0, gt=0)

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: Any) -> Any:
        """Accept `debug` as well as `DEBUG`; anything else unknown is rejected."""
        return value.strip().upper() if isinstance(value, str) else value

    def user_agent_is_placeholder(self) -> bool:
        """True if USER_AGENT still contains placeholder text instead of a real contact."""
        return any(marker in self.user_agent for marker in USER_AGENT_PLACEHOLDER_MARKERS)


@lru_cache
def get_settings() -> Settings:
    """Return the settings, loaded once and cached for the life of the process."""
    return Settings()

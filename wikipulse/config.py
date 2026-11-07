"""All WikiPulse settings, in one place.

Values come from environment variables first, then from a `.env` file in the working
directory, then from the defaults below. Field names map to environment variables
case-insensitively, so `redis_url` is set by `REDIS_URL`.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Text that marks a User-Agent the human has not filled in yet.
USER_AGENT_PLACEHOLDER_MARKERS: tuple[str, ...] = ("<you>", "<your email>", "example.com")

DEFAULT_USER_AGENT = "WikiPulse/0.1 (https://github.com/<you>/wikipulse; <your email>)"


class DetectorConfig(BaseModel):
    """Every threshold of the spike detector (stage 2), in one immutable object.

    Built from the `DETECT_*` settings by `Settings.detector_config()`; tests build it
    directly. See docs/learning/02a-spike-detector.md for why each value was chosen.
    """

    model_config = ConfigDict(frozen=True)

    # Sliding windows: the short one is the "now" signal, the long one delays the baseline.
    short_window_min: float = Field(default=10, gt=0)
    long_window_min: float = Field(default=60, gt=0)
    # A spike starts when, in the short window, a page has at least `min_edits` human
    # edits from at least `min_editors` different people, and at least `min_ratio` times
    # the edits its baseline expects.
    min_edits: int = Field(default=5, ge=1)
    min_editors: int = Field(default=3, ge=1)
    min_ratio: float = Field(default=5.0, gt=0)
    # Second start rule, for stories that build up over an hour rather than 10 minutes:
    # in the long window, at least `long_min_edits` edits from `long_min_editors` people
    # (and `min_ratio` times the expected count for the long window).
    long_min_edits: int = Field(default=10, ge=1)
    long_min_editors: int = Field(default=4, ge=1)
    # Lowest expected count per short window (scaled up for the long window) used in the
    # score, so a page without history doesn't get an infinite score.
    expected_floor: float = Field(default=0.5, gt=0)
    # A running spike stays "hot" while the short window holds at least this many edits;
    # it ends after `quiet_period_min` minutes without a hot moment.
    continue_min_edits: int = Field(default=2, ge=1)
    quiet_period_min: float = Field(default=30, gt=0)
    # Baseline: exponentially decaying average with this time constant (hours).
    baseline_tau_hours: float = Field(default=24, gt=0)
    # Pages whose baseline falls below this many edits per day are forgotten.
    baseline_prune_per_day: float = Field(default=0.05, ge=0)
    # Daily rhythm: overall edit rate over the last `rhythm_tau_hours` compared with the
    # baseline time constant, clamped to [rhythm_min, rhythm_max].
    rhythm_tau_hours: float = Field(default=1, gt=0)
    rhythm_min: float = Field(default=0.5, gt=0)
    rhythm_max: float = Field(default=2.0, gt=0)
    # Count reverts ("Reverted ...", "Undid ...") as normal edits? Default: no.
    count_reverts: bool = False


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
    # Redis Stream that `replay` writes to by default. Kept apart from STREAM_KEY so a
    # replay never puts duplicate events into the live stream.
    replay_stream_key: str = "wiki:replay:edits"
    # Approximate upper bound on the Redis Stream length (XADD MAXLEN ~).
    stream_maxlen: int = Field(default=1_000_000, gt=0)
    # Longest wait between reconnect attempts, in seconds.
    reconnect_max_s: float = Field(default=60.0, gt=0)

    # --- Stage 2: spike detection (thresholds explained in DetectorConfig) ---
    detect_short_window_min: float = Field(default=10, gt=0)
    detect_long_window_min: float = Field(default=60, gt=0)
    detect_min_edits: int = Field(default=5, ge=1)
    detect_min_editors: int = Field(default=3, ge=1)
    detect_min_ratio: float = Field(default=5.0, gt=0)
    detect_long_min_edits: int = Field(default=10, ge=1)
    detect_long_min_editors: int = Field(default=4, ge=1)
    detect_expected_floor: float = Field(default=0.5, gt=0)
    detect_continue_min_edits: int = Field(default=2, ge=1)
    detect_quiet_period_min: float = Field(default=30, gt=0)
    detect_baseline_tau_hours: float = Field(default=24, gt=0)
    detect_baseline_prune_per_day: float = Field(default=0.05, ge=0)
    detect_rhythm_tau_hours: float = Field(default=1, gt=0)
    detect_rhythm_min: float = Field(default=0.5, gt=0)
    detect_rhythm_max: float = Field(default=2.0, gt=0)
    detect_count_reverts: bool = False
    # How often (wall-clock seconds) the detector saves its state to Redis, writes
    # spikes to Postgres and acknowledges the stream entries it has processed.
    detect_snapshot_interval_s: float = Field(default=60, gt=0)

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: Any) -> Any:
        """Accept `debug` as well as `DEBUG`; anything else unknown is rejected."""
        return value.strip().upper() if isinstance(value, str) else value

    def detector_config(self) -> DetectorConfig:
        """The detector thresholds from the `DETECT_*` settings."""
        prefix = "detect_"
        values = {
            name: getattr(self, prefix + name)
            for name in DetectorConfig.model_fields
            if hasattr(self, prefix + name)
        }
        return DetectorConfig(**values)

    def user_agent_is_placeholder(self) -> bool:
        """True if USER_AGENT still contains placeholder text instead of a real contact."""
        return any(marker in self.user_agent for marker in USER_AGENT_PLACEHOLDER_MARKERS)


@lru_cache
def get_settings() -> Settings:
    """Return the settings, loaded once and cached for the life of the process."""
    return Settings()

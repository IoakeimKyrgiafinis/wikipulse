"""Service check: `uv run python -m wikipulse.check`.

Connects to Redis and Postgres, prints their versions, and exits non-zero with a
helpful message if either one is unreachable. Also warns if USER_AGENT is unset.
"""

import logging
import sys

import psycopg
import redis

from wikipulse.config import Settings, get_settings
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5

USER_AGENT_WARNING = (
    "USER_AGENT still contains placeholder text. Wikimedia's User-Agent policy "
    "(https://meta.wikimedia.org/wiki/User-Agent_policy) requires every client to send a "
    "descriptive User-Agent with a way to contact you; clients without one may be blocked. "
    "Set USER_AGENT in .env, e.g. "
    "'WikiPulse/0.1 (https://github.com/<you>/wikipulse; <your email>)' with real values."
)


def redis_version(url: str) -> str:
    """Connect to Redis and return its server version."""
    client = redis.Redis.from_url(
        url,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=CONNECT_TIMEOUT_SECONDS,
    )
    try:
        return str(client.info("server")["redis_version"])
    finally:
        client.close()


def postgres_version(dsn: str) -> str:
    """Connect to Postgres and return its server version."""
    with psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
        row = conn.execute("SHOW server_version").fetchone()
    return str(row[0]) if row else "unknown"


def check_services(settings: Settings) -> list[str]:
    """Check every service; print versions and return the names of unreachable ones."""
    checks = {
        "Redis": lambda: redis_version(settings.redis_url),
        "Postgres": lambda: postgres_version(settings.postgres_dsn),
    }
    failed: list[str] = []
    for name, get_version in checks.items():
        try:
            print(f"{name}: OK (version {get_version()})")
        except Exception as exc:  # any connection problem means "unreachable"
            logger.debug("%s check failed", name, exc_info=True)
            print(f"{name}: UNREACHABLE ({type(exc).__name__}: {exc})")
            failed.append(name)
    return failed


def main() -> int:
    """Run the service check and return a process exit code."""
    setup_logging()
    settings = get_settings()
    if settings.user_agent_is_placeholder():
        print(f"WARNING: {USER_AGENT_WARNING}")
    failed = check_services(settings)
    if failed:
        print(
            f"\nCannot reach: {', '.join(failed)}. "
            "Start the services with `docker compose up -d` and wait until "
            "`docker compose ps` shows them as healthy, then run this check again."
        )
        return 1
    print("\nAll services reachable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

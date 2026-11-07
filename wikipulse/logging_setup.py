"""Logging configuration for WikiPulse entry points."""

import logging

from wikipulse.config import get_settings

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str | None = None) -> None:
    """Configure root logging. Uses LOG_LEVEL from settings unless `level` is given."""
    chosen = (level or get_settings().log_level).upper()
    logging.basicConfig(level=chosen, format=LOG_FORMAT, force=True)

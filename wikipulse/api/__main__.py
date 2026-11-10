"""Start the API and the live page: `uv run python -m wikipulse.api`.

Options override the settings: `--run-id` (API_RUN_ID) picks the grouping run to show,
`--neo4j-uri` another graph, `--host`/`--port` the address (loopback only: the API has no
authentication; in a container, API_LISTEN_ALL=true allows 0.0.0.0, see config.py).
"""

import argparse
import logging
import sys

import uvicorn

from wikipulse.api.app import build_app
from wikipulse.config import Settings, get_settings
from wikipulse.logging_setup import setup_logging
from wikipulse.mcp_server.server import check_host
from wikipulse.metrics.models import register_model_health
from wikipulse.metrics.server import start_metrics_server

logger = logging.getLogger(__name__)

WS_MAX_MESSAGE_BYTES = 64 * 1024  # clients only send "ping"
ALL_INTERFACES = ("0.0.0.0", "::")


def listen_host(host: str, listen_all: bool) -> str:
    """The address to listen on: loopback, or every interface when API_LISTEN_ALL allows it."""
    if listen_all and host in ALL_INTERFACES:
        return host
    return check_host(host)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=None, help="default API_HOST (127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="default API_PORT (8000)")
    parser.add_argument("--run-id", default=None, help="grouping run to show (API_RUN_ID)")
    parser.add_argument("--neo4j-uri", default=None, help="another graph, e.g. the snapshot")
    args = parser.parse_args(argv)
    setup_logging()
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    settings: Settings = get_settings()
    updates = {"api_run_id": args.run_id, "neo4j_uri": args.neo4j_uri}
    settings = settings.model_copy(update={k: v for k, v in updates.items() if v})
    host = listen_host(args.host or settings.api_host, settings.api_listen_all)
    port = args.port or settings.api_port
    logger.info("WikiPulse at http://%s:%d (run %s)", host, port, settings.api_run_id)
    if start_metrics_server(settings) is not None:
        register_model_health(settings.postgres_dsn, settings.metrics_db_connect_timeout_s)
    uvicorn.run(
        build_app(settings),
        host=host,
        port=port,
        log_config=None,  # keep WikiPulse's logging setup
        ws_max_size=WS_MAX_MESSAGE_BYTES,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

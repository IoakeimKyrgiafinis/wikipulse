"""Serve the metrics on `METRICS_PORT`: `GET http://<host>:<port>/metrics`.

Prometheus *pulls*: every 15 to 30 seconds it asks each target for its current values
(a "scrape"). A service only keeps its numbers in memory and answers that request, from a
small HTTP server running in a background thread of its own. So the metrics don't
depend on the service's main loop (a stuck loop still answers, and its frozen counters
show the problem), and the API's Host check and routes are not involved.
"""

import logging
from wsgiref.simple_server import WSGIServer

from prometheus_client import start_http_server

from wikipulse.config import Settings

logger = logging.getLogger(__name__)


def start_metrics_server(settings: Settings) -> WSGIServer | None:
    """Start serving /metrics if METRICS_PORT is set; None otherwise.

    The server thread is a daemon: it ends with the process, nothing to stop.
    """
    if settings.metrics_port is None:
        return None
    server, _thread = start_http_server(settings.metrics_port, addr=settings.metrics_host)
    logger.info("Metrics at http://%s:%d/metrics", settings.metrics_host, server.server_address[1])
    return server

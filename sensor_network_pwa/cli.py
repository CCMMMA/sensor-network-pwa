"""Command-line entry point: development server and anomaly watchdog."""

import argparse
import contextlib
import signal
import sqlite3
import sys
import threading

from sensor_network_pwa.access_store import open_access_store
from sensor_network_pwa.config import load_config
from sensor_network_pwa.influx import init_influx_runtime
from sensor_network_pwa.log import apply_log_level, logger
from sensor_network_pwa.runtime import runtime
from sensor_network_pwa.watchdog import run_watchdog_loop
from sensor_network_pwa.web.app import create_web_app


def parse_args():
    parser = argparse.ArgumentParser(
        description="Web application for the sensor network (reads the collector CSV storage and InfluxDB)"
    )
    parser.add_argument("--config", default="config.json", help="Config file path (default: config.json)")
    parser.add_argument(
        "--watchdog-only",
        action="store_true",
        help="Run only the anomaly watchdog (use next to a Gunicorn deployment of webapp_wsgi)",
    )
    return parser.parse_args()


def shutdown(signum, frame):
    logger.info("Shutting down (signal=%s)...", signum)

    stop_event = runtime.get("watchdog_stop_event")
    if stop_event is not None:
        stop_event.set()

    influx_client = runtime.get("influx_client")
    if influx_client is not None:
        with contextlib.suppress(Exception):
            influx_client.close()

    sys.exit(0)


def main():
    args = parse_args()
    cfg = load_config(args.config)

    apply_log_level(cfg)
    init_influx_runtime(cfg)

    try:
        access_store = open_access_store(cfg)
    except (RuntimeError, OSError, sqlite3.Error) as e:
        logger.error("Cannot open the auth database: %s", e)
        sys.exit(1)
    runtime["access_store"] = access_store

    stop_event = threading.Event()
    runtime["watchdog_stop_event"] = stop_event
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if args.watchdog_only:
        run_watchdog_loop(cfg, access_store, stop_event)
        return

    app = create_web_app(cfg, access_store)
    threading.Thread(
        target=run_watchdog_loop,
        args=(cfg, access_store, stop_event),
        name="pwa-watchdog",
        daemon=True,
    ).start()

    logger.info("Web application listening on http://%s:%s", cfg["http_host"], cfg["http_port"])
    app.run(host=cfg["http_host"], port=cfg["http_port"], debug=False, use_reloader=False, threaded=True)

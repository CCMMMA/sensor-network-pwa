"""Process-wide logging setup."""

import logging
import os

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sensor_network_pwa")


def apply_log_level(cfg: dict):
    logging.getLogger().setLevel(getattr(logging, cfg["log_level"], logging.INFO))

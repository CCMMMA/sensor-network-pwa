import os

from sensor_network_pwa.access_store import open_access_store
from sensor_network_pwa.config import load_config
from sensor_network_pwa.influx import init_influx_runtime
from sensor_network_pwa.log import apply_log_level
from sensor_network_pwa.web import create_web_app


def create_app(config_path: str | None = None):
    # COLLECTOR_CONFIG is still honoured for deployments that predate the split.
    cfg_path = config_path or os.getenv("PWA_CONFIG") or os.getenv("COLLECTOR_CONFIG") or "config.json"
    cfg = load_config(cfg_path)

    apply_log_level(cfg)
    init_influx_runtime(cfg)

    return create_web_app(cfg, open_access_store(cfg))


app = create_app()

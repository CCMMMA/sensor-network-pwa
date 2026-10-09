import os

from main import apply_log_level, create_web_app, init_influx_runtime, load_config, open_access_store


def create_app(config_path: str | None = None):
    # COLLECTOR_CONFIG is still honoured for deployments that predate the split.
    cfg_path = config_path or os.getenv("PWA_CONFIG") or os.getenv("COLLECTOR_CONFIG", "config.json")
    cfg = load_config(cfg_path)

    apply_log_level(cfg)
    init_influx_runtime(cfg)

    return create_web_app(cfg, open_access_store(cfg))


app = create_app()

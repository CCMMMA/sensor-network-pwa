"""Configuration loading: camelCase JSON keys with environment fallbacks, normalised to snake_case."""

import json
import os
from pathlib import Path

from sensor_network_pwa.log import logger


def parse_boolish(value, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    return bool(default)


def cfg_value(raw: dict, keys, env_name=None, default=None):
    for key in keys:
        if key in raw and raw.get(key) is not None:
            return raw.get(key)
    if env_name:
        env = os.getenv(env_name)
        if env is not None:
            return env
    return default


def cfg_required(raw: dict, keys, env_name):
    value = cfg_value(raw, keys, env_name=env_name, default=None)
    if value is None or value == "":
        raise RuntimeError(f"Missing required config key(s) {keys} (or env {env_name})")
    return value


def _text(raw: dict, keys, env_name: str, default: str = "") -> str:
    """A string setting; empty and missing values fall back to the default."""
    return str(cfg_value(raw, keys, env_name=env_name, default=default) or default)


def _web_settings(raw: dict, storage_root) -> dict:
    return {
        "http_host": str(cfg_value(raw, ["httpHost"], env_name="HTTP_HOST", default="0.0.0.0")),
        "http_port": int(cfg_value(raw, ["httpPort"], env_name="HTTP_PORT", default=8080)),
        "auth_db_path": _text(raw, ["authDbPath"], "AUTH_DB_PATH")
        or (str(Path(storage_root) / "collector_auth.sqlite") if storage_root else "collector_auth.sqlite"),
        "web_session_secret": _text(raw, ["webSessionSecret"], "WEB_SESSION_SECRET"),
        "admin_user": _text(raw, ["adminUser"], "ADMIN_USER", "admin"),
        "admin_password": _text(raw, ["adminPassword"], "ADMIN_PASSWORD", "admin"),
        "web_app_logo": _text(raw, ["webAppLogo"], "WEB_APP_LOGO"),
        "web_app_name": _text(raw, ["webAppName"], "WEB_APP_NAME").strip() or "Sensor Network Data Portal",
        "web_app_short_name": _text(raw, ["webAppShortName"], "WEB_APP_SHORT_NAME").strip() or "Sensor Network",
        "web_app_link": _text(raw, ["webAppLink"], "WEB_APP_LINK").strip(),
        "web_info_link": _text(raw, ["webInfoLink"], "WEB_INFO_LINK").strip(),
        "base_url": _text(raw, ["baseUrl"], "BASE_URL").strip(),
    }


def _smtp_settings(raw: dict) -> dict:
    port_raw = cfg_value(raw, ["smtpPort"], env_name="SMTP_PORT", default=None)
    use_tls_raw = cfg_value(raw, ["smtpUseTls"], env_name="SMTP_USE_TLS", default=None)
    user = _text(raw, ["smtpUser"], "SMTP_USER")
    port = int(port_raw) if port_raw not in (None, "") else 25
    if use_tls_raw is None:
        # An unauthenticated relay on port 25 is the one setup that usually has no TLS.
        use_tls = not (port == 25 and not user)
    else:
        use_tls = parse_boolish(use_tls_raw, True)
    settings = {
        "smtp_enabled": parse_boolish(cfg_value(raw, ["smtpEnabled"], env_name="SMTP_ENABLED", default=False), False),
        "smtp_host": _text(raw, ["smtpHost"], "SMTP_HOST").strip(),
        "smtp_port": port,
        "smtp_user": user,
        "smtp_pass": _text(raw, ["smtpPass"], "SMTP_PASS"),
        "smtp_from": _text(raw, ["smtpFrom"], "SMTP_FROM").strip(),
        "smtp_use_tls": use_tls,
    }
    if settings["smtp_enabled"] and not settings["smtp_from"]:
        raise RuntimeError("SMTP enabled but smtpFrom is missing")
    return settings


def _influx_settings(raw: dict) -> dict:
    """InfluxDB is optional here: it is queried only when it is configured."""
    influx_url = cfg_value(raw, ["influxdbUrl", "influxdb_url"], env_name="INFLUXDB_URL", default="")
    enabled = parse_boolish(
        cfg_value(raw, ["influxdb", "influxdb_enabled"], env_name="INFLUXDB_ENABLED", default=bool(influx_url)),
        bool(influx_url),
    )
    settings = {
        "enable_influx": enabled,
        "influx_measurement": str(
            cfg_value(
                raw, ["influxMeasurement", "influx_measurement"], env_name="INFLUX_MEASUREMENT", default="mqtt_data"
            )
        ),
    }
    if enabled:
        settings["influxdb_url"] = str(cfg_required(raw, ["influxdbUrl", "influxdb_url"], "INFLUXDB_URL"))
        settings["influxdb_token"] = str(cfg_required(raw, ["influxdbToken", "influxdb_token"], "INFLUXDB_TOKEN"))
        settings["influxdb_org"] = str(cfg_required(raw, ["influxdbOrg", "influxdb_org"], "INFLUXDB_ORG"))
        settings["influxdb_bucket"] = str(cfg_required(raw, ["influxdbBucket", "influxdb_bucket"], "INFLUXDB_BUCKET"))
    return settings


def _signalk_path_map(raw: dict) -> dict:
    """Only the units in each entry's meta are used here, to label fields in tables and charts."""
    raw_map = cfg_value(raw, ["signalkPathMap", "signalk_path_map"], env_name="SIGNALK_PATH_MAP", default={})
    if isinstance(raw_map, str):
        try:
            raw_map = json.loads(raw_map)
        except json.JSONDecodeError:
            logger.warning("Invalid SIGNALK_PATH_MAP JSON, using empty map")
            raw_map = {}

    parsed_map = {}
    if isinstance(raw_map, dict):
        for k, v in raw_map.items():
            if not isinstance(v, dict):
                continue
            meta = v.get("meta")
            if meta is None and "meta:" in v:
                meta = v.get("meta:")
            meta = sanitize_signalk_meta(meta)
            if meta is not None:
                parsed_map[str(k)] = {"meta": meta}
    return parsed_map


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise RuntimeError("Config file must contain a JSON object")

    # The storage root is written by sensor-network-collector; the web application only reads it.
    storage_root = cfg_value(raw, ["pathStorage", "storage_root"], env_name="STORAGE_ROOT", default=None)

    cfg = {
        "log_level": str(cfg_value(raw, ["logLevel", "log_level"], env_name="LOG_LEVEL", default="INFO")).upper(),
        "storage_root": str(storage_root) if storage_root else None,
        "watchdog_interval_sec": int(
            cfg_value(raw, ["watchdogIntervalSec"], env_name="WATCHDOG_INTERVAL_SEC", default=60)
        ),
        "signalk_path_map": _signalk_path_map(raw),
        "config_path": str(Path(path).resolve()),
    }
    cfg.update(_web_settings(raw, storage_root))
    cfg.update(_influx_settings(raw))
    cfg.update(_smtp_settings(raw))
    if not cfg["base_url"]:
        cfg["base_url"] = f"http://{cfg['http_host']}:{cfg['http_port']}"
    return cfg


# Built-in default and documented sample values of adminPassword.
# Documented sample values of webSessionSecret: public, so never used to sign sessions.
PLACEHOLDER_SESSION_SECRETS = frozenset(
    {
        "replace-with-a-strong-random-secret",
        "replace-with-strong-random-secret",
        "replace-with-strong-secret",
    }
)

DEFAULT_FIELD_UNITS = {
    "TempIn": "C",
    "TempOut": "C",
    "HumIn": "%",
    "HumOut": "%",
    "Barometer": "hPa",
    "BarTrend": "hPa/h",
    "WindSpeed": "m/s",
    "WindSpeed10Min": "m/s",
    "WindDir": "deg",
    "RainRate": "mm/h",
    "RainStorm": "mm",
    "RainDay": "mm",
    "RainMonth": "mm",
    "RainYear": "mm",
    "ETDay": "mm",
    "ETMonth": "mm",
    "ETYear": "mm",
    "SolarRad": "W/m2",
    "BatteryVolts": "V",
    "temp": "C",
    "heat_index": "C",
    "dew_point": "C",
    "wet_bulb": "C",
    "hum": "%",
    "bar": "hPa",
    "pm_1": "ug/m3",
    "pm_2p5": "ug/m3",
    "pm_10": "ug/m3",
    "pm_2p5_1_hour": "ug/m3",
    "pm_2p5_3_hour": "ug/m3",
    "pm_2p5_24_hour": "ug/m3",
    "pm_2p5_nowcast": "ug/m3",
    "pm_10_1_hour": "ug/m3",
    "pm_10_3_hour": "ug/m3",
    "pm_10_24_hour": "ug/m3",
    "pm_10_nowcast": "ug/m3",
    "aqi_val": "AQI",
    "aqi_1_hour_val": "AQI",
    "aqi_nowcast_val": "AQI",
}


def get_field_units(cfg: dict):
    out = dict(DEFAULT_FIELD_UNITS)
    path_map = cfg.get("signalk_path_map", {})
    for key, entry in path_map.items() if isinstance(path_map, dict) else []:
        key_str = str(key)
        if key_str in DEFAULT_FIELD_UNITS:
            continue
        if isinstance(entry, dict):
            meta = entry.get("meta")
            if isinstance(meta, dict):
                units = meta.get("units")
                if isinstance(units, str) and units.strip():
                    out[key_str] = units.strip()
    return out


def sanitize_signalk_meta(meta):
    if not isinstance(meta, dict):
        return None
    cleaned = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        cleaned[str(key)] = value
    return cleaned or None

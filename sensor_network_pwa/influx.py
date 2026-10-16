"""Optional InfluxDB access for the public dashboard."""

import json
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient

from sensor_network_pwa.intervals import normalize_public_window
from sensor_network_pwa.log import logger
from sensor_network_pwa.runtime import runtime


def _influx_range_start_for_window(window: str):
    window = normalize_public_window(window)
    if window == "1m":
        return "-10m"
    if window == "10m":
        return "-2h"
    if window == "hour":
        return "-12h"
    if window == "3h":
        return "-24h"
    if window == "6h":
        return "-48h"
    if window == "12h":
        return "-72h"
    if window == "24h":
        return "-7d"
    if window == "72h":
        return "-14d"
    if window == "week":
        return "-21d"
    return "-14d"


def query_influx_station_rows(cfg: dict, instrument_uuid: str, window: str, start: datetime | None = None):
    if not cfg or not cfg.get("enable_influx"):
        return []
    influx_client = runtime.get("influx_client")
    if influx_client is None:
        return []

    # Query from the start of the displayed window when it is known; the relative
    # ranges are much wider, to cover stations whose latest data is old.
    if start is not None:
        range_start = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        range_start = _influx_range_start_for_window(window)
    measurement = json.dumps(cfg["influx_measurement"])
    station = json.dumps(instrument_uuid)
    query = f"""
from(bucket: {json.dumps(cfg["influxdb_bucket"])})
  |> range(start: {range_start})
  |> filter(fn: (r) => r._measurement == {measurement} and r.uuid == {station})
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
    try:
        tables = influx_client.query_api().query(query=query, org=cfg["influxdb_org"])
    except Exception as e:
        logger.warning("Influx query failed for public dashboard station=%s: %s", instrument_uuid, e)
        return []

    out = []
    for table in tables:
        for record in table.records:
            values = dict(record.values or {})
            ts = values.get("_time")
            if isinstance(ts, datetime):
                ts_iso = ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            else:
                ts_iso = str(ts or "")
            row = {"timestamp": ts_iso, "uuid": instrument_uuid}
            for key, value in values.items():
                if key.startswith("_") or key in ("result", "table"):
                    continue
                row[key] = value
            out.append(row)
    out.sort(key=lambda row: row.get("timestamp", ""))
    return out


def init_influx_runtime(cfg: dict):
    runtime["config"] = cfg
    if not cfg.get("enable_influx"):
        return None

    influx_client = runtime.get("influx_client")
    if influx_client is not None:
        return influx_client

    influx_client = InfluxDBClient(
        url=cfg["influxdb_url"],
        token=cfg["influxdb_token"],
        org=cfg["influxdb_org"],
    )
    runtime["influx_client"] = influx_client
    return influx_client

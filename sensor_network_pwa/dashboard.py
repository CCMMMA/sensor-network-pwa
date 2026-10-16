"""Model of the public station dashboard."""

import math

from sensor_network_pwa.access_store import AccessStore
from sensor_network_pwa.charts import (
    PUBLIC_CARD_LOOKBACK_ROWS,
    PUBLIC_METRIC_SPECS,
    PUBLIC_SERIES_SPECS,
    calc_axis_settings,
    first_numeric_for_aliases,
    resolve_station_chart_specs,
)
from sensor_network_pwa.influx import query_influx_station_rows
from sensor_network_pwa.intervals import interval_start, normalize_public_window
from sensor_network_pwa.runtime import runtime
from sensor_network_pwa.storage import get_station_preview, load_station_rows, to_float
from sensor_network_pwa.timeutil import parse_iso_ts, utc_iso, utc_now


def _decimate_timeseries(rows_ts, max_points: int):
    if not max_points or len(rows_ts) <= max_points:
        return rows_ts
    step = max(1, math.ceil(len(rows_ts) / max_points))
    sampled = rows_ts[::step]
    if sampled and sampled[-1] != rows_ts[-1]:
        sampled.append(rows_ts[-1])
    return sampled


def _merge_station_rows(*row_sets):
    merged = {}
    for rows in row_sets:
        for row in rows or []:
            ts = str(row.get("timestamp") or "").strip()
            if not ts:
                continue
            if ts not in merged:
                merged[ts] = dict(row)
            else:
                combined = dict(row)
                combined.update({k: v for k, v in merged[ts].items() if v is not None and v != ""})
                merged[ts] = combined
    return [merged[k] for k in sorted(merged.keys())]


def _aqi_status(value):
    v = to_float(value)
    if v is None:
        return None
    if v <= 50:
        return {"level": "good", "label": "Good conditions", "color": "#198754"}
    if v <= 100:
        return {"level": "warning", "label": "Warning conditions", "color": "#ffc107"}
    return {"level": "bad", "label": "Bad conditions", "color": "#dc3545"}


def _build_series_stats(points, label: str = ""):
    clean: list[dict] = []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        y = to_float(point.get("y"))
        x = str(point.get("x") or "").strip()
        if y is None or not x:
            continue
        clean.append({"x": x, "y": float(y)})
    if not clean:
        return None
    min_point = min(clean, key=lambda item: item["y"])
    max_point = max(clean, key=lambda item: item["y"])
    current_point = clean[-1]
    return {
        "label": label,
        "current": round(current_point["y"], 3),
        "current_at": current_point["x"],
        "min": round(min_point["y"], 3),
        "min_at": min_point["x"],
        "max": round(max_point["y"], 3),
        "max_at": max_point["x"],
    }


PARTICULATE_SERIES_SPECS = [
    {"label": "PM1", "aliases": ["pm_1", "PM1"]},
    {"label": "PM2.5", "aliases": ["pm_2p5", "pm2_5", "PM2_5", "PM2.5"]},
    {"label": "PM10", "aliases": ["pm_10", "PM10"]},
]
AQI_ALIASES = ["aqi_val", "AQI", "CurrentAQI", "aqi"]
WIND_DIRECTION_ALIASES = ["WindDir", "wind_dir"]
WIND_SPEED_ALIASES = ["WindSpeed", "wind_speed"]


def _series_samples(rows_ts, aliases):
    """(ISO timestamp, value) of every row that carries one of the aliases."""
    samples = []
    for ts, row in rows_ts:
        value = first_numeric_for_aliases(row, aliases)
        if value is not None:
            samples.append((utc_iso(ts), value))
    return samples


def _points(samples):
    return [{"x": iso_ts, "y": round(value, 3)} for iso_ts, value in samples]


def _load_snapshot_rows(cfg: dict, storage_root: str, instrument_uuid: str, window: str, preview: dict):
    preview_ts = parse_iso_ts(preview.get("last_timestamp") or "")
    latest_hint = preview_ts or utc_now()
    storage_window_start = interval_start(latest_hint, window)
    # Only the hourly files overlapping the window are read.
    storage_rows = load_station_rows(
        storage_root,
        instrument_uuid,
        from_date=storage_window_start,
        to_date=latest_hint,
        limit=None,
    )
    influx_rows = query_influx_station_rows(
        cfg, instrument_uuid, window, start=storage_window_start if preview_ts else None
    )
    return _merge_station_rows(influx_rows, storage_rows)


def _empty_snapshot(instrument_uuid: str, window: str, preview: dict):
    return {
        "instrument_uuid": instrument_uuid,
        "station_name": preview.get("name") or instrument_uuid,
        "last_timestamp": None,
        "rows": 0,
        "window_start": None,
        "window_end": None,
        "location": {
            "latitude": preview.get("latitude"),
            "longitude": preview.get("longitude"),
        },
        "cards": [],
        "series": [],
        "aqi_status": None,
        "window": window,
    }


def _latest_value(recent_rows, aliases):
    for row in recent_rows:
        value = first_numeric_for_aliases(row, aliases)
        if value is not None:
            return value
    return None


def _build_cards(recent_rows):
    cards = []
    for spec in PUBLIC_METRIC_SPECS:
        value = _latest_value(recent_rows, spec["aliases"])
        cards.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "value": None if value is None else round(value, 2),
                "unit": spec["unit"],
            }
        )
    return cards


def _build_single_series(rows_ts, chart_specs):
    series = []
    for spec in PUBLIC_SERIES_SPECS:
        samples = _series_samples(rows_ts, spec["aliases"])
        if not samples:
            continue
        resolved_spec = chart_specs.get(spec["key"], spec)
        points = _points(samples)
        values = [point["y"] for point in points]
        y_min, y_max, y_step = calc_axis_settings(values, resolved_spec.get("axis"))
        series.append(
            {
                "key": spec["key"],
                "label": resolved_spec["label"],
                "unit": resolved_spec["unit"],
                "y_min": y_min,
                "y_max": y_max,
                "y_step": y_step,
                "labels": [point["x"] for point in points],
                "values": values,
                "points": points,
                "stats": _build_series_stats(points, resolved_spec["label"]),
            }
        )
    return series


def _build_wind_series(rows_ts, chart_specs):
    """Wind direction (line, left axis) and speed (bars, right axis) on one chart; None without wind data."""
    dir_points = _points(_series_samples(rows_ts, WIND_DIRECTION_ALIASES))
    speed_points = _points(_series_samples(rows_ts, WIND_SPEED_ALIASES))
    if not dir_points and not speed_points:
        return None
    default_specs = {spec["key"]: spec for spec in PUBLIC_SERIES_SPECS}
    dir_spec = chart_specs.get("wind_direction", default_specs.get("wind_direction", {}))
    speed_spec = chart_specs.get("wind_speed", default_specs.get("wind_speed", {}))
    dir_min, dir_max, dir_step = calc_axis_settings([p["y"] for p in dir_points], dir_spec.get("axis"))
    speed_min, speed_max, speed_step = calc_axis_settings([p["y"] for p in speed_points], speed_spec.get("axis"))
    stats = [_build_series_stats(dir_points, "Wind Direction"), _build_series_stats(speed_points, "Wind Speed")]
    return {
        "key": "wind_combined",
        "label": "Wind",
        "unit": "",
        "labels": [utc_iso(ts) for ts, _ in rows_ts],
        "datasets": [
            {
                "label": "Wind Direction",
                "points": dir_points,
                "type": "line",
                "yAxisID": "wind_direction",
            },
            {
                "label": "Wind Speed",
                "points": speed_points,
                "type": "bar",
                "yAxisID": "wind_speed",
            },
        ],
        "axes": {
            "wind_direction": {
                "position": "left",
                "unit": dir_spec.get("unit", "deg"),
                "y_min": dir_min,
                "y_max": dir_max,
                "y_step": dir_step,
            },
            "wind_speed": {
                "position": "right",
                "unit": speed_spec.get("unit", "m/s"),
                "y_min": speed_min,
                "y_max": speed_max,
                "y_step": speed_step,
            },
        },
        "stats": [item for item in stats if item],
    }


def _build_particulate_series(rows_ts, chart_specs):
    """PM1, PM2.5 and PM10 on one chart; None when the station reports none of them."""
    labels: list[str] = []
    datasets = []
    all_values: list[float] = []
    for item in PARTICULATE_SERIES_SPECS:
        samples = _series_samples(rows_ts, item["aliases"])
        if not samples:
            continue
        points = _points(samples)
        all_values.extend(float(value) for _, value in samples)
        if len(points) > len(labels):
            labels = [point["x"] for point in points]
        datasets.append({"label": item["label"], "values": [point["y"] for point in points], "points": points})
    if not datasets:
        return None
    spec = chart_specs.get("particulate_matter", {"axis": {"auto": True, "floor_zero": True}})
    y_min, y_max, y_step = calc_axis_settings(all_values, spec.get("axis"))
    stats = [_build_series_stats(dataset["points"], dataset["label"]) for dataset in datasets]
    return {
        "key": "particulate_matter",
        "label": spec.get("label", "Particulate Matter"),
        "unit": spec.get("unit", "ug/m3"),
        "y_min": y_min,
        "y_max": y_max,
        "y_step": y_step,
        "labels": labels,
        "datasets": datasets,
        "stats": [item for item in stats if item],
    }


def build_public_station_snapshot(
    storage_root: str,
    instrument_uuid: str,
    window: str = "hour",
    max_points: int = 240,
    cfg: dict | None = None,
    access_store: AccessStore | None = None,
):
    window = normalize_public_window(window)
    cfg = cfg or runtime.get("config") or {}
    access_store = access_store or runtime.get("access_store")
    preview = get_station_preview(storage_root, instrument_uuid)
    rows = _load_snapshot_rows(cfg, storage_root, instrument_uuid, window, preview)
    if not rows:
        return _empty_snapshot(instrument_uuid, window, preview)

    rows_ts_all = []
    for row in rows:
        ts = parse_iso_ts(row.get("timestamp", ""))
        if ts is not None:
            rows_ts_all.append((ts, row))
    rows_ts_all.sort(key=lambda x: x[0])

    latest_row = rows_ts_all[-1][1] if rows_ts_all else rows[-1]
    latest_dt = rows_ts_all[-1][0] if rows_ts_all else parse_iso_ts(latest_row.get("timestamp", "")) or utc_now()

    win_start = interval_start(latest_dt, window)
    rows_ts = _decimate_timeseries([(ts, row) for ts, row in rows_ts_all if ts >= win_start], max_points)

    # A station can interleave rows from different devices (weather, air quality), so
    # a value missing from the latest row is taken from the few rows before it.
    recent_rows = [row for _, row in reversed(rows_ts_all[-PUBLIC_CARD_LOOKBACK_ROWS:])] or [latest_row]

    chart_specs = {spec["key"]: spec for spec in resolve_station_chart_specs(access_store, instrument_uuid)}
    series = _build_single_series(rows_ts, chart_specs)
    for combined in (_build_wind_series(rows_ts, chart_specs), _build_particulate_series(rows_ts, chart_specs)):
        if combined:
            series.append(combined)

    return {
        "instrument_uuid": instrument_uuid,
        "station_name": latest_row.get("name") or preview.get("name") or instrument_uuid,
        "last_timestamp": utc_iso(latest_dt),
        "rows": len(rows_ts),
        "window": window,
        "window_start": utc_iso(win_start),
        "window_end": utc_iso(latest_dt),
        "location": {
            "latitude": preview.get("latitude"),
            "longitude": preview.get("longitude"),
        },
        "cards": _build_cards(recent_rows),
        "series": series,
        "aqi_status": _aqi_status(_latest_value(recent_rows, AQI_ALIASES)),
    }

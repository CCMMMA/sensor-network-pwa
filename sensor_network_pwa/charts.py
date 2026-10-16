"""Chart catalogue, per-station chart settings, axis scaling and table statistics."""

import math
import re
import statistics

from sensor_network_pwa.access_store import AccessStore
from sensor_network_pwa.storage import to_float
from sensor_network_pwa.timeutil import now_utc_iso

DEFAULT_CHART_COLORS = [
    "#0b57d0",
    "#1f9d55",
    "#d95f02",
    "#7b1fa2",
    "#c2185b",
    "#00838f",
    "#5d4037",
    "#455a64",
]


def normalize_chart_color(value, fallback="#0b57d0"):
    raw = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return raw.lower()
    return fallback


def build_table_column_stats(rows, columns):
    stats = []
    for col in columns:
        samples = []
        for row in rows:
            value = to_float(row.get(col))
            if value is None:
                continue
            timestamp = str(row.get("timestamp") or "")
            samples.append({"value": value, "timestamp": timestamp})
        values = [sample["value"] for sample in samples]
        if not values:
            continue
        min_sample = min(samples, key=lambda sample: sample["value"])
        max_sample = max(samples, key=lambda sample: sample["value"])
        avg = statistics.fmean(values)
        stddev = statistics.pstdev(values) if len(values) > 1 else 0.0
        stats.append(
            {
                "column": col,
                "min": round(min_sample["value"], 6),
                "min_at": min_sample["timestamp"],
                "max": round(max_sample["value"], 6),
                "max_at": max_sample["timestamp"],
                "avg": round(avg, 6),
                "stddev": round(stddev, 6),
                "count": len(values),
            }
        )
    return stats


PUBLIC_METRIC_SPECS = [
    {
        "key": "temperature",
        "label": "Temperature",
        "aliases": ["TempOut", "temperature", "outside_temp", "temp"],
        "unit": "C",
    },
    {"key": "humidity", "label": "Humidity", "aliases": ["HumOut", "humidity", "hum"], "unit": "%"},
    {"key": "pressure", "label": "Pressure", "aliases": ["Barometer", "pressure", "bar"], "unit": "hPa"},
    {"key": "wind_speed", "label": "Wind Speed", "aliases": ["WindSpeed", "wind_speed"], "unit": "m/s"},
    {"key": "wind_direction", "label": "Wind Direction", "aliases": ["WindDir", "wind_dir"], "unit": "deg"},
    {"key": "rain_rate", "label": "Rain Rate", "aliases": ["RainRate", "rain_rate"], "unit": "mm/h"},
    {"key": "aqi_current", "label": "Current AQI", "aliases": ["aqi_val", "AQI", "CurrentAQI", "aqi"], "unit": ""},
    {"key": "aqi_1h", "label": "1 Hour AQI", "aliases": ["aqi_1_hour_val", "AQI1h", "AQI_1h"], "unit": ""},
    {"key": "aqi_nowcast", "label": "NowCast AQI", "aliases": ["aqi_nowcast_val", "NowCastAQI"], "unit": ""},
    {"key": "pm1", "label": "PM1", "aliases": ["pm_1", "PM1"], "unit": "ug/m3"},
    {"key": "pm2_5", "label": "PM2.5", "aliases": ["pm_2p5", "pm2_5", "PM2_5", "PM2.5"], "unit": "ug/m3"},
    {"key": "pm10", "label": "PM10", "aliases": ["pm_10", "PM10"], "unit": "ug/m3"},
]

# Rows searched, newest first, for the current value of a dashboard card.
PUBLIC_CARD_LOOKBACK_ROWS = 10

PUBLIC_SERIES_SPECS = [
    {
        "key": "temperature",
        "label": "Temperature Trend",
        "aliases": ["TempOut", "temperature", "outside_temp", "temp"],
        "unit": "C",
        "axis": {"auto": True},
    },
    {
        "key": "humidity",
        "label": "Humidity Trend",
        "aliases": ["HumOut", "humidity", "hum"],
        "unit": "%",
        "axis": {"min": 0, "max": 100},
    },
    {
        "key": "pressure",
        "label": "Pressure Trend",
        "aliases": ["Barometer", "pressure", "bar"],
        "unit": "hPa",
        "axis": {"auto": True},
    },
    {
        "key": "wind_speed",
        "label": "Wind Speed Trend",
        "aliases": ["WindSpeed", "wind_speed"],
        "unit": "m/s",
        "axis": {"auto": True, "floor_zero": True},
    },
    {
        "key": "wind_direction",
        "label": "Wind Direction Trend",
        "aliases": ["WindDir", "wind_dir"],
        "unit": "deg",
        "axis": {"min": 0, "max": 360},
    },
    {
        "key": "rain_rate",
        "label": "Rain Rate Trend",
        "aliases": ["RainRate", "rain_rate"],
        "unit": "mm/h",
        "axis": {"auto": True, "floor_zero": True},
    },
    {
        "key": "aqi_trend",
        "label": "Air Quality Index",
        "aliases": ["aqi_val", "AQI", "CurrentAQI", "aqi"],
        "unit": "",
        "axis": {"min": 0, "max": 300},
    },
]

PUBLIC_EXTRA_CHART_SPECS: list[dict] = [
    {
        "key": "particulate_matter",
        "label": "Particulate Matter",
        "unit": "ug/m3",
        "axis": {"auto": True, "floor_zero": True},
    },
]


def first_numeric_for_aliases(row: dict, aliases):
    for alias in aliases:
        if alias in row:
            v = to_float(row.get(alias))
            if v is not None:
                return v
    lowered = {str(k).lower(): k for k in row}
    for alias in aliases:
        key = lowered.get(str(alias).lower())
        if key is None:
            continue
        v = to_float(row.get(key))
        if v is not None:
            return v
    return None


def _coerce_axis_value(value):
    if value in ("", None):
        return None
    return to_float(value)


def _nice_axis_step(raw_step: float):
    if raw_step <= 0:
        return 1.0
    exponent = math.floor(math.log10(raw_step))
    fraction = raw_step / (10**exponent)
    if fraction <= 1:
        nice_fraction = 1
    elif fraction <= 2:
        nice_fraction = 2
    elif fraction <= 5:
        nice_fraction = 5
    else:
        nice_fraction = 10
    return nice_fraction * (10**exponent)


def calc_axis_settings(values, axis_spec=None):
    axis_spec = axis_spec or {}
    explicit_min = _coerce_axis_value(axis_spec.get("min")) if "min" in axis_spec else None
    explicit_max = _coerce_axis_value(axis_spec.get("max")) if "max" in axis_spec else None
    explicit_step = _coerce_axis_value(axis_spec.get("step")) if "step" in axis_spec else None
    auto = bool(axis_spec.get("auto"))

    if not values and explicit_min is None and explicit_max is None:
        return None, None, explicit_step

    if values:
        lo = min(values)
        hi = max(values)
    else:
        lo = explicit_min if explicit_min is not None else 0.0
        hi = explicit_max if explicit_max is not None else lo + 1.0

    if lo == hi:
        pad = max(1.0, abs(lo) * 0.15)
        lo -= pad
        hi += pad
    elif auto or explicit_min is None or explicit_max is None:
        pad = (hi - lo) * 0.15
        lo -= pad
        hi += pad

    if axis_spec.get("floor_zero"):
        lo = max(0.0, lo)

    if explicit_step is not None and explicit_step > 0:
        step = explicit_step
    else:
        span = max(hi - lo, 1e-9)
        target_ticks = max(3, int(axis_spec.get("ticks") or 6))
        step = _nice_axis_step(span / target_ticks)

    if explicit_min is not None:
        y_min = explicit_min
    else:
        y_min = math.floor(lo / step) * step
        if axis_spec.get("floor_zero"):
            y_min = max(0.0, y_min)

    if explicit_max is not None:
        y_max = explicit_max
    else:
        y_max = math.ceil(hi / step) * step

    if y_min == y_max:
        y_max = y_min + step

    return round(y_min, 6), round(y_max, 6), round(step, 6)


def get_chart_setting_catalog():
    out = []
    for spec in PUBLIC_SERIES_SPECS + PUBLIC_EXTRA_CHART_SPECS:
        out.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "unit": spec.get("unit", ""),
                "aliases": list(spec.get("aliases") or []),
                "axis": dict(spec.get("axis") or {}),
            }
        )
    return out


def resolve_station_chart_specs(access_store: AccessStore | None, instrument_uuid: str):
    overrides = access_store.get_station_chart_settings(instrument_uuid) if access_store else {}
    resolved = []
    for spec in get_chart_setting_catalog():
        merged_axis = dict(spec.get("axis") or {})
        override = overrides.get(spec["key"]) or {}
        if override.get("y_min") is not None:
            merged_axis["min"] = float(override["y_min"])
        if override.get("y_max") is not None:
            merged_axis["max"] = float(override["y_max"])
        if override.get("y_step") is not None:
            merged_axis["step"] = float(override["y_step"])
        resolved.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "unit": spec.get("unit", ""),
                "aliases": list(spec.get("aliases") or []),
                "axis": merged_axis,
                "saved": {
                    "y_min": override.get("y_min"),
                    "y_max": override.get("y_max"),
                    "y_step": override.get("y_step"),
                    "updated_at": override.get("updated_at"),
                    "updated_by": override.get("updated_by"),
                },
            }
        )
    return resolved


def normalize_station_chart_settings_map(raw_settings):
    valid_keys = {spec["key"] for spec in get_chart_setting_catalog()}
    out = {}
    for series_key, raw in (raw_settings or {}).items():
        key = str(series_key or "").strip()
        if not key or key not in valid_keys:
            continue
        item = raw or {}
        y_min = _coerce_axis_value(item.get("y_min"))
        y_max = _coerce_axis_value(item.get("y_max"))
        y_step = _coerce_axis_value(item.get("y_step"))
        if y_step is not None and y_step <= 0:
            raise ValueError(f"Invalid y_step for {key}")
        if y_min is not None and y_max is not None and y_max <= y_min:
            raise ValueError(f"y_max must be greater than y_min for {key}")
        if y_min is None and y_max is None and y_step is None:
            continue
        out[key] = {
            "y_min": y_min,
            "y_max": y_max,
            "y_step": y_step,
        }
    return out


def export_station_chart_settings_payload(access_store: AccessStore, station_uuid: str):
    saved = access_store.get_station_chart_settings(station_uuid) if access_store else {}
    series = {}
    for spec in get_chart_setting_catalog():
        item = saved.get(spec["key"]) or {}
        series[spec["key"]] = {
            "label": spec["label"],
            "unit": spec.get("unit", ""),
            "y_min": item.get("y_min"),
            "y_max": item.get("y_max"),
            "y_step": item.get("y_step"),
            "updated_at": item.get("updated_at"),
            "updated_by": item.get("updated_by"),
        }
    return {
        "station_uuid": station_uuid,
        "exported_at": now_utc_iso(),
        "series": series,
    }


def parse_station_chart_settings_payload(payload: dict):
    if not isinstance(payload, dict):
        raise ValueError("Invalid JSON payload")
    raw_series = payload.get("series")
    if not isinstance(raw_series, dict):
        raise ValueError("Missing 'series' object")
    normalized_input = {}
    for series_key, raw in raw_series.items():
        if not isinstance(raw, dict):
            continue
        normalized_input[series_key] = {
            "y_min": raw.get("y_min"),
            "y_max": raw.get("y_max"),
            "y_step": raw.get("y_step"),
        }
    return normalize_station_chart_settings_map(normalized_input)

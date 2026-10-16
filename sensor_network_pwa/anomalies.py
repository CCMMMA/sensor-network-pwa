"""Station anomaly evaluation and the administration network overview."""

from datetime import datetime

from sensor_network_pwa.storage import (
    collect_instruments,
    get_station_preview,
    is_missing_sensor_value,
    load_station_rows,
    to_float,
)
from sensor_network_pwa.timeutil import parse_iso_ts, utc_now


def _normalize_alarm_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value).strip().lower()


def _battery_alarms_from_row(row: dict):
    alarms = []
    battery_info = []
    for key, raw in row.items():
        lk = str(key).lower()
        if "battery" not in lk:
            continue

        fval = to_float(raw)
        sval = _normalize_alarm_value(raw)
        if fval is not None:
            battery_info.append(f"{key}={round(fval, 3)}")
        elif sval:
            battery_info.append(f"{key}={raw}")

        if (
            "volt" in lk
            and fval is not None
            and fval < 3.0
            or any(t in lk for t in ("status", "level", "percent", "pct"))
            and fval is not None
            and fval <= 20
            or any(t in lk for t in ("status", "state"))
            and sval in ("low", "critical", "bad", "false", "0")
        ):
            alarms.append(f"low_battery:{key}")
    return alarms, battery_info


def compute_station_usual_update_seconds(rows):
    timestamps = []
    for row in rows:
        ts = parse_iso_ts(row.get("timestamp", ""))
        if ts is not None:
            timestamps.append(ts)
    timestamps.sort()
    if len(timestamps) < 3:
        return None
    deltas = []
    for i in range(1, len(timestamps)):
        delta = (timestamps[i] - timestamps[i - 1]).total_seconds()
        if delta > 0:
            deltas.append(delta)
    if len(deltas) < 2:
        return None
    deltas.sort()
    return int(deltas[len(deltas) // 2])


# Without enough rows to learn a station's update rate, it is late after this many seconds.
DEFAULT_FAILURE_THRESHOLD_SECONDS = 15 * 60
# Columns that identify a row, and text too long to be a reading, are not sensor values.
NON_SENSOR_COLUMNS = ("timestamp", "topic", "uuid", "name")
MAX_SENSOR_TEXT_LENGTH = 180


def _latest_row(rows):
    latest_ts = None
    latest_row = None
    for row in rows:
        ts = parse_iso_ts(row.get("timestamp", ""))
        if ts is not None and (latest_ts is None or ts > latest_ts):
            latest_ts = ts
            latest_row = row
    return latest_ts, latest_row


def _expected_sensor_keys(rows):
    """Every sensor column the station has reported in the given rows."""
    expected_keys = set()
    for row in rows:
        for key, raw in row.items():
            if key in NON_SENSOR_COLUMNS:
                continue
            if isinstance(raw, str) and len(raw) > MAX_SENSOR_TEXT_LENGTH:
                continue
            expected_keys.add(key)
    return expected_keys


def _sensor_alarms(latest_row, expected_keys):
    """Alarms, present values and missing columns of the latest row."""
    alarms = []
    values = {}
    missing_fields = []
    numeric_count = 0
    for key in expected_keys:
        raw = latest_row.get(key)
        if is_missing_sensor_value(raw):
            missing_fields.append(key)
            continue
        values[key] = raw
        if to_float(raw) is not None:
            numeric_count += 1
    if numeric_count < 2:
        alarms.append("sensor_failure:too_few_numeric_values")
    if missing_fields:
        short = ",".join(missing_fields[:8])
        suffix = "" if len(missing_fields) <= 8 else ",..."
        alarms.append(f"sensor_failure:missing_values[{len(missing_fields)}]={short}{suffix}")
    return alarms, values, missing_fields


def evaluate_station_anomalies(station_uuid: str, rows, now_dt: datetime):
    latest_ts, latest_row = _latest_row(rows)
    expected_keys = _expected_sensor_keys(rows)
    usual_update_seconds = compute_station_usual_update_seconds(rows)
    failure_threshold_seconds = None
    if usual_update_seconds is not None:
        failure_threshold_seconds = max(2 * usual_update_seconds, 60)

    alarms = []
    values = {}
    battery_info = []
    missing_fields = []
    if latest_ts is None or latest_row is None:
        alarms.append("lost_connectivity:no_data")
    else:
        age_seconds = int((now_dt - latest_ts).total_seconds())
        if failure_threshold_seconds is None:
            failure_threshold_seconds = DEFAULT_FAILURE_THRESHOLD_SECONDS
        if age_seconds > failure_threshold_seconds:
            alarms.append(f"lost_connectivity:{age_seconds}s(threshold={failure_threshold_seconds}s)")

        batt_alarms, battery_info = _battery_alarms_from_row(latest_row)
        alarms.extend(batt_alarms)
        sensor_alarms, values, missing_fields = _sensor_alarms(latest_row, expected_keys)
        alarms.extend(sensor_alarms)

    return {
        "latest_ts": latest_ts,
        "values": values,
        "battery_info": battery_info,
        "missing_fields": missing_fields,
        "expected_keys": expected_keys,
        "alarms": alarms,
        "usual_update_seconds": usual_update_seconds,
        "failure_threshold_seconds": failure_threshold_seconds,
    }


def build_admin_network_dashboard(storage_root: str):
    now = utc_now()
    stations = []
    value_keys = set()

    for instrument_uuid in collect_instruments(storage_root):
        preview = get_station_preview(storage_root, instrument_uuid)
        rows = load_station_rows(storage_root, instrument_uuid, limit=500)
        summary = evaluate_station_anomalies(instrument_uuid, rows, now)
        latest_ts = summary["latest_ts"]
        value_keys.update(summary["expected_keys"])

        stations.append(
            {
                "uuid": instrument_uuid,
                "name": preview.get("name") or instrument_uuid,
                "lastTimestamp": latest_ts.isoformat().replace("+00:00", "Z") if latest_ts else None,
                "ageSeconds": (int((now - latest_ts).total_seconds()) if latest_ts else None),
                "usualUpdateSeconds": summary["usual_update_seconds"],
                "failureThresholdSeconds": summary["failure_threshold_seconds"],
                "status": "ALARM" if summary["alarms"] else "OK",
                "alarms": summary["alarms"],
                "batteryInfo": ", ".join(summary["battery_info"]) if summary["battery_info"] else "",
                "missingFields": summary["missing_fields"],
                "missingCount": len(summary["missing_fields"]),
                "values": summary["values"],
            }
        )

    stations.sort(key=lambda s: (0 if s["status"] == "ALARM" else 1, s["uuid"]))
    value_columns = sorted(value_keys)
    return {
        "updatedAt": now.isoformat().replace("+00:00", "Z"),
        "valueColumns": value_columns,
        "stations": stations,
    }


def anomaly_base_type(anomaly_code: str):
    return str(anomaly_code or "").split(":", 1)[0]

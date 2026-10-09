"""Readers for the hourly CSV storage written by sensor-network-collector:
<pathStorage>/<station>/YYYY/MM/DD/<station>_YYYYMMDDZHH00.csv.
"""

import csv
import json
import math
import re
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def collect_instruments(storage_root: str):
    root = Path(storage_root)
    if not root.exists() or not root.is_dir():
        return []
    instruments = []
    for p in root.iterdir():
        if not p.is_dir():
            continue
        if p.name.startswith("_") or p.name.startswith("."):
            continue
        if next(p.rglob("*.csv"), None) is None:
            continue
        instruments.append(p.name)
    return sorted(instruments)


def find_latest_csv_file(storage_root: str, instrument_uuid: str):
    root = Path(storage_root) / instrument_uuid
    if not root.exists() or not root.is_dir():
        return None

    def _descending_dirs(parent: Path):
        dirs = [p for p in parent.iterdir() if p.is_dir() and not p.name.startswith(".")]
        return sorted(dirs, key=lambda p: p.name, reverse=True)

    for year_dir in _descending_dirs(root):
        for month_dir in _descending_dirs(year_dir):
            for day_dir in _descending_dirs(month_dir):
                csv_files = sorted(
                    [p for p in day_dir.iterdir() if p.is_file() and p.suffix.lower() == ".csv"],
                    key=lambda p: p.name,
                    reverse=True,
                )
                if csv_files:
                    return csv_files[0]

    for path in root.rglob("*.csv"):
        return path
    return None


def iter_csv_rows(csv_path: Path):
    # csv.reader with zip() is about twice as fast as csv.DictReader on the hourly files.
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return
        width = len(header)
        for values in reader:
            if not values:
                continue
            if len(values) < width:
                padding: list[str | None] = [None] * (width - len(values))
                yield dict(zip(header, [*values, *padding], strict=False))
            else:
                # zip() drops the surplus values of a row longer than the header.
                yield dict(zip(header, values, strict=False))


def read_latest_station_row(csv_path: Path):
    try:
        last_row = None
        for row in iter_csv_rows(csv_path):
            last_row = row
        return last_row
    except Exception:
        return None


def extract_date_from_name(name: str):
    # UUID_YYYYMMDDZHH00.csv
    m = re.search(r"_(\d{8})Z\d{4}\.csv$", name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


_CSV_FILE_HOUR_RE = re.compile(r"_(\d{4})(\d{2})(\d{2})Z(\d{2})\d{2}\.csv$")


def _csv_file_hour(name: str):
    """Start of the hour covered by UUID_YYYYMMDDZHH00.csv, or None."""
    m = _CSV_FILE_HOUR_RE.search(name)
    if not m:
        return None
    try:
        year, month, day, hour = (int(g) for g in m.groups())
        return datetime(year, month, day, hour, tzinfo=timezone.utc)
    except ValueError:
        return None


def as_utc_bound(value, end_of_day: bool):
    """Accept a date or a datetime as a time bound."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    moment = datetime.max.time() if end_of_day else datetime.min.time()
    return datetime.combine(value, moment, tzinfo=timezone.utc)


def iter_csv_files_newest_first(storage_root: str, instrument_uuid: str, from_date=None, to_date=None):
    """Yield the station's hourly files, newest first.

    Year, month and day directories outside the requested period are not opened, so
    the cost depends on the period and not on the length of the station's history.
    """
    root = Path(storage_root) / instrument_uuid
    start = as_utc_bound(from_date, end_of_day=False)
    end = as_utc_bound(to_date, end_of_day=True)
    start_key = (start.year, start.month, start.day) if start else None
    end_key = (end.year, end.month, end.day) if end else None

    def walk(directory: Path, date_key: tuple | None):
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name, reverse=True)
        except OSError:
            return
        files = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                key: tuple | None = date_key
                # <station>/YYYY/MM/DD: compare as much of the date as the path gives.
                if key is not None and len(key) < 3 and entry.name.isdigit():
                    key = key + (int(entry.name),)
                    if start_key and key < start_key[: len(key)]:
                        continue
                    if end_key and key > end_key[: len(key)]:
                        continue
                else:
                    key = None
                yield from walk(entry, key)
            elif entry.suffix.lower() == ".csv":
                if start or end:
                    hour = _csv_file_hour(entry.name)
                    if hour is None:
                        continue
                    if start and hour + timedelta(hours=1) <= start:
                        continue
                    if end and hour > end:
                        continue
                files.append(entry)
        # Files next to date directories do not follow the layout: treat them as oldest.
        yield from files

    if root.is_dir():
        yield from walk(root, ())


def list_csv_files_for_instrument(storage_root: str, instrument_uuid: str, from_date=None, to_date=None):
    return sorted(iter_csv_files_newest_first(storage_root, instrument_uuid, from_date=from_date, to_date=to_date))


def make_zip_for_download(storage_root: str, instrument_uuids, from_date=None, to_date=None):
    with tempfile.NamedTemporaryFile(prefix="collector_download_", suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    count = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for instrument_uuid in instrument_uuids:
                files = list_csv_files_for_instrument(
                    storage_root, instrument_uuid, from_date=from_date, to_date=to_date
                )
                for f in files:
                    rel = f.relative_to(Path(storage_root))
                    zf.write(f, arcname=str(rel))
                    count += 1
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return tmp_path, count


def to_float(value):
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            out = float(value)
        else:
            s = str(value).strip()
            if not s:
                return None
            out = float(s)
        # NaN/inf readings are not plottable and break axis and JSON handling.
        return out if math.isfinite(out) else None
    except Exception:
        return None


def is_missing_sensor_value(value):
    if value is None:
        return True
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "nan", "none", "null", "n/a", "na", "-"):
            return True
    return isinstance(value, float) and math.isnan(value)


def _extract_lat_lon_from_row(row: dict):
    # Direct latitude/longitude columns
    key_pairs = [
        ("latitude", "longitude"),
        ("lat", "lon"),
        ("lat", "lng"),
    ]
    for k_lat, k_lon in key_pairs:
        lat = to_float(row.get(k_lat))
        lon = to_float(row.get(k_lon))
        if lat is not None and lon is not None:
            return lat, lon

    # Nested JSON in 'position' column
    pos_raw = row.get("position")
    if isinstance(pos_raw, str) and pos_raw.strip().startswith("{"):
        try:
            pos = json.loads(pos_raw)
            lat = to_float(pos.get("latitude"))
            lon = to_float(pos.get("longitude"))
            if lat is not None and lon is not None:
                return lat, lon
        except Exception:
            pass

    return None, None


def get_station_preview(storage_root: str, instrument_uuid: str):
    latest_csv = find_latest_csv_file(storage_root, instrument_uuid)
    if latest_csv is None:
        return {"latitude": None, "longitude": None, "last_timestamp": None, "rows": 0, "name": instrument_uuid}

    latest_row = read_latest_station_row(latest_csv)
    if not latest_row:
        return {"latitude": None, "longitude": None, "last_timestamp": None, "rows": 0, "name": instrument_uuid}

    lat, lon = _extract_lat_lon_from_row(latest_row)
    station_name = instrument_uuid
    maybe_name = latest_row.get("name")
    if isinstance(maybe_name, str) and maybe_name.strip():
        station_name = maybe_name.strip()

    return {
        "latitude": lat,
        "longitude": lon,
        "last_timestamp": latest_row.get("timestamp"),
        "rows": 0,
        "name": station_name,
    }


def load_station_rows(storage_root: str, instrument_uuid: str, from_date=None, to_date=None, limit=400):
    """Rows in time order. from_date/to_date are dates or datetimes selecting the hourly files."""
    limited = limit is not None and limit > 0
    chunks = []
    total = 0
    # Newest files first, so a limited read neither lists nor parses the whole history.
    for csv_path in iter_csv_files_newest_first(storage_root, instrument_uuid, from_date=from_date, to_date=to_date):
        try:
            chunk = list(iter_csv_rows(csv_path))
        except Exception:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if limited and total >= limit:
            break

    out = [row for chunk in reversed(chunks) for row in chunk]
    if limited and len(out) > limit:
        out = out[-limit:]
    return out


def extract_numeric_series(rows, excluded=None):
    excluded = set(excluded or [])
    numeric_keys = set()
    for row in rows:
        for k, v in row.items():
            if k in excluded:
                continue
            if to_float(v) is not None:
                numeric_keys.add(k)
    return sorted(numeric_keys)

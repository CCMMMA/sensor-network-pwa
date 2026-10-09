"""Station data pages: browsing, CSV export, downloads, logos and chart settings."""

import contextlib
import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, Response, abort, g, redirect, render_template, request, send_file, url_for

from sensor_network_pwa.charts import (
    DEFAULT_CHART_COLORS,
    build_table_column_stats,
    export_station_chart_settings_payload,
    get_chart_setting_catalog,
    normalize_station_chart_settings_map,
    parse_station_chart_settings_payload,
    resolve_station_chart_specs,
)
from sensor_network_pwa.config import get_field_units, parse_boolish
from sensor_network_pwa.dashboard import build_public_station_snapshot
from sensor_network_pwa.intervals import TREND_INTERVALS, interval_start, normalize_interval, shift_anchor
from sensor_network_pwa.storage import (
    as_utc_bound,
    extract_numeric_series,
    get_station_preview,
    load_station_rows,
    make_zip_for_download,
    to_float,
)
from sensor_network_pwa.timeutil import parse_date_ymd, parse_iso_ts, utc_iso, utc_now
from sensor_network_pwa.validation import safe_filename
from sensor_network_pwa.web.access import (
    app_logo_path,
    available_instruments,
    current_user,
    get_cfg,
    get_store,
    login_required,
    logo_url,
    redirect_to_login,
    station_is_accessible,
    station_is_controllable,
    station_logo_dir,
    storage_root_or_404,
)
from sensor_network_pwa.web.util import json_for_script, request_preference_cookie

STATION_BROWSER_MAX_CHART_POINTS = 3000

STATION_BROWSER_INTERVALS = frozenset({code for code, _ in TREND_INTERVALS} | {"custom"})
STATION_BROWSER_PAGE_SIZES = ("50", "100", "250", "1000")
STATION_BROWSER_MAX_CUSTOM_DAYS = 31


# Columns that describe the row rather than a measurement: never offered as chart series.
NON_SERIES_COLUMNS = frozenset({"timestamp", "topic", "uuid", "position", "latitude", "longitude", "lat", "lon", "lng"})


@dataclass
class StationWindow:
    """The time range selected on the station page and the rows inside it."""

    user: dict | None
    preview: dict
    latest_dt: datetime
    interval: str
    win_start: datetime
    win_end: datetime
    # Anchors step a named interval; ranges step a custom one. The other pair is None.
    prev_anchor: datetime | None = None
    next_anchor: datetime | None = None
    prev_range: tuple | None = None
    next_range: tuple | None = None
    rows: list = field(default_factory=list)

    def use_end(self, win_end: datetime):
        """Move a named interval so that it ends at win_end."""
        self.win_end = win_end
        self.win_start = interval_start(win_end, self.interval)
        self.prev_anchor = shift_anchor(win_end, self.interval, -1)
        self.next_anchor = shift_anchor(win_end, self.interval, 1)


def _requested_window(user, preview: dict) -> StationWindow:
    """Time range named by the request arguments (or the interval cookie), before any row is read."""
    latest_dt = parse_iso_ts(preview.get("last_timestamp") or "") or utc_now()
    from_date = parse_date_ymd(request.args.get("from_date", ""))
    to_date = parse_date_ymd(request.args.get("to_date", ""))
    if from_date and to_date and to_date < from_date:
        abort(400, "to_date must be >= from_date")

    interval = request_preference_cookie(request, "interval", "station_trend_window", normalize_interval, "hour")
    if from_date or to_date:
        interval = "custom"
    if interval not in STATION_BROWSER_INTERVALS:
        interval = "hour"

    try:
        if interval != "custom":
            win_end = parse_iso_ts(request.args.get("anchor", "")) or latest_dt
            window = StationWindow(user, preview, latest_dt, interval, win_end, win_end)
            window.use_end(win_end)
            return window

        win_end = as_utc_bound(to_date, end_of_day=True) if to_date else latest_dt
        win_start = as_utc_bound(from_date, end_of_day=False) if from_date else win_end - timedelta(days=1)
        if win_end < win_start:
            win_end = win_start + timedelta(days=1)
        if win_end - win_start > timedelta(days=STATION_BROWSER_MAX_CUSTOM_DAYS + 1):
            abort(
                400,
                f"A custom range can cover at most {STATION_BROWSER_MAX_CUSTOM_DAYS} days; "
                "use the ZIP download for longer periods",
            )
        span = win_end - win_start
        return StationWindow(
            user,
            preview,
            latest_dt,
            interval,
            win_start,
            win_end,
            prev_range=(win_start - span, win_start),
            next_range=(win_end, win_end + span),
        )
    except OverflowError:
        abort(400, "The requested time range is out of bounds")


def _load_window_rows(window: StationWindow, storage_root: str, instrument_uuid: str):
    loaded = []
    for row in load_station_rows(
        storage_root, instrument_uuid, from_date=window.win_start, to_date=window.win_end, limit=None
    ):
        ts = parse_iso_ts(row.get("timestamp", ""))
        if ts is not None:
            loaded.append((ts, row))
    if window.interval != "custom" and not request.args.get("anchor") and loaded:
        # The latest window ends at the newest row, which need not be the last line of the file.
        newest = max(ts for ts, _ in loaded)
        if newest > window.win_end:
            window.latest_dt = newest
            window.use_end(newest)
    in_range = [(ts, row) for ts, row in loaded if window.win_start <= ts <= window.win_end]
    window.rows = [row for _, row in sorted(in_range, key=lambda item: item[0])]


def _thin_for_chart(rows):
    """Long windows hold tens of thousands of samples: the chart gets an evenly thinned
    series, while the table, the statistics and the CSV keep every row."""
    chart_step = max(1, math.ceil(len(rows) / STATION_BROWSER_MAX_CHART_POINTS))
    chart_rows = rows[::chart_step]
    if chart_step > 1 and chart_rows[-1] is not rows[-1]:
        chart_rows.append(rows[-1])
    return chart_rows, chart_step


def _table_page(rows) -> dict:
    """Page of table rows selected by the request arguments (or the page-size and order cookies)."""
    page_size = int(
        request_preference_cookie(
            request,
            "page_size",
            "station_page_size",
            lambda v: v if v in STATION_BROWSER_PAGE_SIZES else "50",
            "50",
        )
    )
    order = request_preference_cookie(
        request, "order", "station_row_order", lambda v: "desc" if v == "desc" else "asc", "asc"
    )
    try:
        page = max(1, int(request.args.get("page", "1") or "1"))
    except ValueError:
        page = 1
    total_rows = len(rows)
    page_count = max(1, (total_rows + page_size - 1) // page_size)
    page = min(page, page_count)
    start_idx = (page - 1) * page_size
    ordered_rows = rows[::-1] if order == "desc" else rows
    return {
        "table_rows": ordered_rows[start_idx : start_idx + page_size],
        "page": page,
        "page_count": page_count,
        "page_size": page_size,
        "order": order,
        "total_rows": total_rows,
        "start_idx": start_idx,
        "end_idx": min(start_idx + page_size, total_rows),
    }


def _range_link_args(window: StationWindow) -> dict:
    """Arguments that identify the current, previous and next time range, reused by every link of the page."""
    if window.interval == "custom":
        assert window.prev_range and window.next_range  # set together with a custom interval
        return {
            "range_args": {
                "from_date": window.win_start.date().isoformat(),
                "to_date": window.win_end.date().isoformat(),
            },
            "prev_args": {
                "from_date": window.prev_range[0].date().isoformat(),
                "to_date": (window.prev_range[1] - timedelta(seconds=1)).date().isoformat(),
            },
            "next_args": {
                "from_date": (window.next_range[0] + timedelta(seconds=1)).date().isoformat(),
                "to_date": window.next_range[1].date().isoformat(),
            },
        }
    assert window.prev_anchor and window.next_anchor  # set together with a named interval
    range_args = {"interval": window.interval}
    if request.args.get("anchor"):
        range_args["anchor"] = utc_iso(window.win_end)
    return {
        "range_args": range_args,
        "prev_args": {"interval": window.interval, "anchor": utc_iso(window.prev_anchor)},
        "next_args": {"interval": window.interval, "anchor": utc_iso(window.next_anchor)},
    }


def _csv_chunks(rows, columns):
    """The rows as CSV text, yielded every 500 rows so that long exports stream."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for index, row in enumerate(rows, start=1):
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in columns])
        if index % 500 == 0:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
    yield buffer.getvalue()


bp = Blueprint("stations", __name__)


def load_station_window(instrument_uuid: str):
    """Access check and rows of the time range selected by the request arguments.

    Used by the station page and by its CSV export, so both show the same rows.
    Returns a StationWindow, or the redirect to the login page.
    """
    user = current_user()
    storage_root = storage_root_or_404()
    if instrument_uuid not in set(available_instruments(storage_root)):
        abort(404, "Station not found")
    if not station_is_accessible(user, instrument_uuid):
        if user is None:
            return redirect_to_login("Please log in to browse this station.")
        abort(403)

    window = _requested_window(user, get_station_preview(storage_root, instrument_uuid))
    _load_window_rows(window, storage_root, instrument_uuid)
    return window


@bp.route("/station/<path:instrument_uuid>/export.csv")
def export_station_csv(instrument_uuid: str):
    window = load_station_window(instrument_uuid)
    if not isinstance(window, StationWindow):
        return window
    rows = window.rows
    all_columns = list(dict.fromkeys(key for row in rows for key in row))
    wanted = [c for c in request.args.getlist("col") if c in all_columns]
    if not rows:
        abort(404, "No data rows in the selected time range")

    stamp = "%Y%m%dT%H%M%SZ"
    filename = safe_filename(
        f"{instrument_uuid}_{window.win_start.strftime(stamp)}_{window.win_end.strftime(stamp)}.csv"
    )
    response = Response(_csv_chunks(rows, wanted or all_columns), mimetype="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@bp.route("/station/<path:instrument_uuid>")
def browse_station(instrument_uuid: str):
    window = load_station_window(instrument_uuid)
    if not isinstance(window, StationWindow):
        return window
    rows = window.rows
    station_logo_row = get_store().get_station_logo(instrument_uuid)
    station_logo_url = None
    if station_logo_row:
        station_logo_url = url_for("public.asset_file", kind="station", name=Path(station_logo_row["logo_path"]).name)

    numeric_cols = extract_numeric_series(rows, excluded=NON_SERIES_COLUMNS)
    units_map = get_field_units(get_cfg())
    chart_rows, chart_step = _thin_for_chart(rows)
    # Hourly files can have different headers, so use every column seen in the window.
    all_table_columns = list(dict.fromkeys(key for row in rows for key in row))

    return render_template(
        "stations/station_browser.html",
        instrument_uuid=instrument_uuid,
        station_name=window.preview.get("name") or instrument_uuid,
        user=window.user,
        can_control=station_is_controllable(window.user, instrument_uuid),
        app_logo_url=logo_url(app_logo_path()),
        station_logo_url=station_logo_url,
        interval=window.interval,
        interval_options=TREND_INTERVALS,
        chart_step=chart_step,
        win_start=utc_iso(window.win_start),
        win_end=utc_iso(window.win_end),
        is_latest=window.win_end >= window.latest_dt,
        numeric_cols=numeric_cols,
        default_chart_colors=DEFAULT_CHART_COLORS,
        all_table_columns=all_table_columns,
        table_column_stats=build_table_column_stats(rows, numeric_cols),
        units_map=units_map,
        page_sizes=STATION_BROWSER_PAGE_SIZES,
        max_custom_days=STATION_BROWSER_MAX_CUSTOM_DAYS,
        station_browse_state_json=json_for_script(
            {
                "chart_labels": [str(row.get("timestamp") or "") for row in chart_rows],
                "numeric_cols": numeric_cols,
                "all_table_columns": all_table_columns,
                "units_map": units_map,
                "numeric_series_aligned": {
                    column: [to_float(row.get(column)) for row in chart_rows] for column in numeric_cols
                },
            }
        ),
        **_range_link_args(window),
        **_table_page(rows),
    )


@bp.route("/station/<path:instrument_uuid>/logo", methods=["POST"])
@login_required
def upload_station_logo(instrument_uuid: str):
    user = g.user
    storage_root = storage_root_or_404()
    instruments = set(available_instruments(storage_root))
    if instrument_uuid not in instruments:
        abort(404, "Station not found")
    if not station_is_accessible(user, instrument_uuid):
        abort(403)

    f = request.files.get("logo")
    if f is None or not f.filename:
        abort(400, "Missing logo file")
    original = safe_filename(f.filename)
    ext = Path(original).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        abort(400, "Unsupported logo format")

    content = f.read()
    if not content:
        abort(400, "Empty logo file")
    digest = hashlib.sha256(content).hexdigest()[:12]
    out_name = safe_filename(f"{instrument_uuid}_{digest}{ext}")
    out_path = station_logo_dir() / out_name
    out_path.write_bytes(content)
    get_store().set_station_logo(instrument_uuid, str(out_path), user["username"])
    return redirect(url_for("stations.browse_station", instrument_uuid=instrument_uuid))


@bp.route("/download", methods=["POST"])
def download():
    user = current_user()
    storage_root = storage_root_or_404()
    all_instruments = set(available_instruments(storage_root))

    requested = []
    for inst in request.form.getlist("instrument"):
        inst = str(inst).strip()
        if inst and inst not in requested:
            requested.append(inst)

    if not requested:
        abort(400, "Select at least one instrument")

    unknown = [inst for inst in requested if inst not in all_instruments]
    if unknown:
        abort(404, f"Unknown station(s): {', '.join(unknown)}")

    unauthorized = [inst for inst in requested if not station_is_accessible(user, inst)]
    if unauthorized:
        abort(403, f"Access denied for station(s): {', '.join(unauthorized)}")

    from_date = parse_date_ymd(request.form.get("from_date", ""))
    to_date = parse_date_ymd(request.form.get("to_date", ""))
    if from_date and to_date and to_date < from_date:
        abort(400, "to_date must be >= from_date")

    zip_path, count = make_zip_for_download(storage_root, requested, from_date=from_date, to_date=to_date)
    if count == 0:
        zip_path.unlink(missing_ok=True)
        abort(404, "No data files found for selected filters")

    response = send_file(
        zip_path,
        as_attachment=True,
        download_name=f"collector_data_{datetime.now().strftime('%Y%m%dT%H%M%S')}.zip",
        mimetype="application/zip",
    )
    # send_file() already holds the archive open, so on POSIX it can be removed
    # now and the space is released once the response has been streamed.
    with contextlib.suppress(OSError):
        zip_path.unlink(missing_ok=True)
    return response


@bp.route("/station/<path:instrument_uuid>/chart-settings", methods=["GET", "POST"])
@login_required
def station_chart_settings(instrument_uuid: str):
    user = g.user

    storage_root = storage_root_or_404()
    instruments = set(available_instruments(storage_root))
    if instrument_uuid not in instruments:
        abort(404, "Station not found")
    if not station_is_controllable(user, instrument_uuid):
        abort(403)

    msg = ""
    err = ""
    if request.method == "POST":
        raw_settings = {}
        for spec in get_chart_setting_catalog():
            raw_settings[spec["key"]] = {
                "y_min": request.form.get(f"y_min__{spec['key']}", ""),
                "y_max": request.form.get(f"y_max__{spec['key']}", ""),
                "y_step": request.form.get(f"y_step__{spec['key']}", ""),
            }
        try:
            normalized = normalize_station_chart_settings_map(raw_settings)
            get_store().replace_station_chart_settings(instrument_uuid, normalized, user["username"])
            msg = "Trend chart settings saved"
        except ValueError as e:
            err = str(e)

    preview = get_station_preview(storage_root, instrument_uuid)
    snapshot = build_public_station_snapshot(
        storage_root,
        instrument_uuid,
        window="hour",
        max_points=120,
        cfg=get_cfg(),
        access_store=get_store(),
    )
    effective_series = {item["key"]: item for item in snapshot.get("series", [])}
    chart_specs = resolve_station_chart_specs(get_store(), instrument_uuid)
    return render_template(
        "stations/station_chart_settings.html",
        instrument_uuid=instrument_uuid,
        station_name=preview.get("name") or instrument_uuid,
        chart_specs=chart_specs,
        effective_series=effective_series,
        msg=msg,
        err=err,
    )


@bp.route("/station/<path:instrument_uuid>/chart-settings/export")
@login_required
def station_chart_settings_export(instrument_uuid: str):
    user = g.user
    storage_root = storage_root_or_404()
    instruments = set(available_instruments(storage_root))
    if instrument_uuid not in instruments:
        abort(404, "Station not found")
    if not station_is_controllable(user, instrument_uuid):
        abort(403)

    payload = export_station_chart_settings_payload(get_store(), instrument_uuid)
    response = Response(
        response=json.dumps(payload, indent=2, sort_keys=True),
        status=200,
        mimetype="application/json",
    )
    response.headers["Content-Disposition"] = (
        f"attachment; filename={safe_filename(instrument_uuid)}_chart_settings.json"
    )
    return response


@bp.route("/station/<path:instrument_uuid>/chart-settings/import", methods=["POST"])
@login_required
def station_chart_settings_import(instrument_uuid: str):
    user = g.user
    storage_root = storage_root_or_404()
    instruments = set(available_instruments(storage_root))
    if instrument_uuid not in instruments:
        abort(404, "Station not found")
    if not station_is_controllable(user, instrument_uuid):
        abort(403)

    upload = request.files.get("settings_file")
    if upload is None or not upload.filename:
        abort(400, "Missing JSON file")
    try:
        payload = json.load(upload.stream)
        if not isinstance(payload, dict):
            raise ValueError("Invalid JSON payload")
        payload_station_uuid = str(payload.get("station_uuid") or "").strip()
        foreign = payload_station_uuid and payload_station_uuid != instrument_uuid
        if foreign and not parse_boolish(request.form.get("confirm_foreign_station", "0"), False):
            abort(
                400,
                f"JSON file belongs to station {payload_station_uuid}. "
                "Confirm import from another station in the web form and retry.",
            )
        normalized = parse_station_chart_settings_payload(payload)
        get_store().replace_station_chart_settings(instrument_uuid, normalized, user["username"])
    except ValueError as e:
        abort(400, str(e))
    except json.JSONDecodeError:
        abort(400, "Invalid JSON file")
    return redirect(url_for("stations.station_chart_settings", instrument_uuid=instrument_uuid))

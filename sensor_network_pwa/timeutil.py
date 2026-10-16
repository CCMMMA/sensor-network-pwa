"""UTC timestamp helpers shared by the store, the readers and the web layer."""

from datetime import datetime, timezone


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now():
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def parse_date_ymd(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_iso_ts(value: str):
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def shift_months(dt: datetime, months: int):
    y = dt.year + ((dt.month - 1 + months) // 12)
    m = ((dt.month - 1 + months) % 12) + 1
    d = min(
        dt.day,
        [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][
            m - 1
        ],
    )
    return dt.replace(year=y, month=m, day=d)

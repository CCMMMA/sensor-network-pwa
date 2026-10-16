"""Time windows offered by the station pages and the public dashboard."""

from datetime import datetime, timedelta

from sensor_network_pwa.timeutil import shift_months

TREND_INTERVALS = [
    ("1m", "last minute"),
    ("10m", "10 minutes"),
    ("hour", "hour"),
    ("3h", "3 hours"),
    ("6h", "6 hours"),
    ("12h", "12 hours"),
    ("24h", "24 hours"),
    ("72h", "72 hours"),
    ("week", "one week"),
]

PUBLIC_TREND_WINDOWS = [
    ("1m", "last minute"),
    ("10m", "10 minutes"),
    ("hour", "hour"),
    ("3h", "3 hours"),
    ("6h", "6 hours"),
    ("12h", "12 hours"),
    ("24h", "24 hours"),
    ("72h", "72 hours"),
    ("week", "one week"),
]


def normalize_interval(value: str):
    raw = (value or "").strip().lower()
    aliases = {
        "1m": "1m",
        "last_minute": "1m",
        "10m": "10m",
        "10minutes": "10m",
        "hour": "hour",
        "1h": "hour",
        "3h": "3h",
        "6h": "6h",
        "12h": "12h",
        "24h": "24h",
        "day": "24h",
        "72h": "72h",
        "week": "week",
        "1w": "week",
        "month": "month",
        "year": "year",
        "custom": "custom",
    }
    return aliases.get(raw, "hour")


def normalize_public_window(value: str):
    normalized = normalize_interval(value)
    allowed = {k for k, _ in PUBLIC_TREND_WINDOWS}
    return normalized if normalized in allowed else "hour"


def interval_start(anchor: datetime, interval: str):
    interval = normalize_interval(interval)
    if interval == "1m":
        return anchor - timedelta(minutes=1)
    if interval == "10m":
        return anchor - timedelta(minutes=10)
    if interval == "hour":
        return anchor - timedelta(hours=1)
    if interval == "3h":
        return anchor - timedelta(hours=3)
    if interval == "6h":
        return anchor - timedelta(hours=6)
    if interval == "12h":
        return anchor - timedelta(hours=12)
    if interval == "24h":
        return anchor - timedelta(days=1)
    if interval == "72h":
        return anchor - timedelta(hours=72)
    if interval == "week":
        return anchor - timedelta(weeks=1)
    if interval == "month":
        return shift_months(anchor, -1)
    if interval == "year":
        return shift_months(anchor, -12)
    return anchor - timedelta(days=1)


def shift_anchor(anchor: datetime, interval: str, steps: int):
    interval = normalize_interval(interval)
    if interval == "1m":
        return anchor + timedelta(minutes=steps)
    if interval == "10m":
        return anchor + timedelta(minutes=10 * steps)
    if interval == "hour":
        return anchor + timedelta(hours=steps)
    if interval == "3h":
        return anchor + timedelta(hours=3 * steps)
    if interval == "6h":
        return anchor + timedelta(hours=6 * steps)
    if interval == "12h":
        return anchor + timedelta(hours=12 * steps)
    if interval == "24h":
        return anchor + timedelta(days=steps)
    if interval == "72h":
        return anchor + timedelta(hours=72 * steps)
    if interval == "week":
        return anchor + timedelta(weeks=steps)
    if interval == "month":
        return shift_months(anchor, steps)
    if interval == "year":
        return shift_months(anchor, 12 * steps)
    return anchor + timedelta(days=steps)

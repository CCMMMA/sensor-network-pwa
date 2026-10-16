"""Process-wide runtime state shared by the entry points."""

from typing import Any

runtime: dict[str, Any] = {
    "config": {},
    "influx_client": None,
    "access_store": None,
    "watchdog_stop_event": None,
}

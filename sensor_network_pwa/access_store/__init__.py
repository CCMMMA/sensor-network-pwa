"""SQLite auth database: users, account requests, tokens, station policies, anomalies, notifications, logos,
chart settings and the generated session secret.

Each concern is a mixin over StoreBase; AccessStore combines them.
"""

from sensor_network_pwa.access_store.account_requests import AccountRequestsMixin
from sensor_network_pwa.access_store.anomalies import AnomaliesMixin
from sensor_network_pwa.access_store.notifications import (
    DEFAULT_NOTIFICATION_INTERVAL_MIN,
    NOTIFICATION_INTERVAL_CHOICES,
    NOTIFICATION_LINK_TTL_MIN,
    NOTIFICATION_SNOOZE_HOURS,
    NotificationsMixin,
    normalize_notification_interval,
)
from sensor_network_pwa.access_store.station_settings import StationSettingsMixin
from sensor_network_pwa.access_store.tokens import TokensMixin

__all__ = [
    "DEFAULT_NOTIFICATION_INTERVAL_MIN",
    "NOTIFICATION_INTERVAL_CHOICES",
    "NOTIFICATION_LINK_TTL_MIN",
    "NOTIFICATION_SNOOZE_HOURS",
    "AccessStore",
    "normalize_notification_interval",
    "open_access_store",
]


class AccessStore(
    AccountRequestsMixin,
    TokensMixin,
    NotificationsMixin,
    AnomaliesMixin,
    StationSettingsMixin,
):
    """The auth database. Users and permissions come in through the mixins that build on them."""


def open_access_store(cfg: dict) -> AccessStore:
    access_store = AccessStore(cfg["auth_db_path"])
    access_store.ensure_admin(cfg["admin_user"], cfg["admin_password"])
    return access_store

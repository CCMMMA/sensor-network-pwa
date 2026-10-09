"""Flask application factory."""

from flask import Flask

from sensor_network_pwa.access_store import AccessStore
from sensor_network_pwa.config import PLACEHOLDER_SESSION_SECRETS
from sensor_network_pwa.log import logger
from sensor_network_pwa.runtime import runtime
from sensor_network_pwa.web import admin, auth, hooks, profile, public, pwa, stations
from sensor_network_pwa.web.access import CONFIG_KEY, STORE_EXTENSION

BLUEPRINTS = (pwa.bp, public.bp, stations.bp, auth.bp, admin.bp, profile.bp)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _session_secret(cfg: dict, access_store: AccessStore) -> str:
    session_secret = cfg["web_session_secret"]
    if not session_secret or session_secret in PLACEHOLDER_SESSION_SECRETS:
        # Kept in the auth DB so that all Gunicorn workers and restarts share it.
        session_secret = access_store.get_or_create_session_secret()
        logger.warning(
            "webSessionSecret is not set or is a sample value; using a generated secret stored in the auth DB"
        )
    return session_secret


def create_web_app(cfg: dict, access_store: AccessStore) -> Flask:
    """Build the application for one configuration and auth store."""
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_session_secret(cfg, access_store),
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    )
    app.config[CONFIG_KEY] = cfg
    app.extensions[STORE_EXTENSION] = access_store
    # The public-dashboard model falls back to this store when called outside a request.
    runtime["access_store"] = access_store

    hooks.init_app(app)
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
    return app

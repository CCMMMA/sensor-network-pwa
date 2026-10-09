"""Flask application factory."""

from flask import Flask

from sensor_network_pwa.access_store import AccessStore
from sensor_network_pwa.config import PLACEHOLDER_SESSION_SECRETS
from sensor_network_pwa.log import logger
from sensor_network_pwa.runtime import runtime
from sensor_network_pwa.web import admin, auth, hooks, profile, public, pwa, stations
from sensor_network_pwa.web.context import WebContext

# Hooks first: before_request hooks run in registration order.
ROUTE_MODULES = (hooks, pwa, public, stations, auth, admin, profile)


def create_web_app(cfg: dict, access_store: AccessStore):
    app = Flask(__name__)
    session_secret = cfg["web_session_secret"]
    if not session_secret or session_secret in PLACEHOLDER_SESSION_SECRETS:
        # Kept in the auth DB so that all Gunicorn workers and restarts share it.
        session_secret = access_store.get_or_create_session_secret()
        logger.warning(
            "webSessionSecret is not set or is a sample value; using a generated secret stored in the auth DB"
        )
    app.secret_key = session_secret
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
    runtime["access_store"] = access_store

    ctx = WebContext(cfg, access_store)
    for module in ROUTE_MODULES:
        module.register(app, ctx)
    return app

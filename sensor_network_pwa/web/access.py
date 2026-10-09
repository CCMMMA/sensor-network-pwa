"""Per-request access to the configuration, the auth store and the signed-in user.

The application factory stores the configuration and the store on the app; the views
reach them through these helpers, so no module holds global application state.
"""

import functools
import mimetypes
from pathlib import Path

from flask import abort, current_app, g, redirect, request, send_file, session, url_for

from sensor_network_pwa.access_store import AccessStore
from sensor_network_pwa.storage import collect_instruments

CONFIG_KEY = "PORTAL_CONFIG"
STORE_EXTENSION = "access_store"


def get_cfg() -> dict:
    """The dictionary returned by load_config for this application."""
    return current_app.config[CONFIG_KEY]


def get_store() -> AccessStore:
    return current_app.extensions[STORE_EXTENSION]


def app_logo_path() -> str:
    return str(get_cfg().get("web_app_logo") or "").strip()


def send_image_asset(path: Path):
    mime, _ = mimetypes.guess_type(str(path))
    response = send_file(path, mimetype=mime or "application/octet-stream")
    # Uploaded SVG files may carry scripts; never let them run in this origin.
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return response


def current_request_target() -> str:
    target = request.full_path or request.path or url_for("public.index")
    return target[:-1] if target.endswith("?") else target


def redirect_to_login(message: str):
    return redirect(url_for("auth.login", next=current_request_target(), err=message))


def current_user():
    """The active user of the session, or None."""
    username = session.get("username")
    if not username:
        return None
    user = get_store().get_user(username)
    return user if user and int(user.get("active") or 0) == 1 else None


def login_required(view):
    """Send anonymous visitors to the login page; the view finds the user in g.user."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect_to_login("Please log in to continue.")
        g.user = user
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    """Like login_required, and refuse users who are not administrators."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect_to_login("Please log in to access this page.")
        if user.get("role") != "admin":
            abort(403)
        g.user = user
        return view(*args, **kwargs)

    return wrapped


def storage_root_or_404() -> str:
    storage_root = get_cfg().get("storage_root")
    if not storage_root:
        abort(404, "Storage is not enabled/configured")
    return storage_root


def available_instruments(storage_root: str):
    return collect_instruments(storage_root)


def station_is_accessible(user, instrument_uuid: str):
    return get_store().can_download(user, instrument_uuid)


def station_is_public(user, instrument_uuid: str):
    # Restricted stations are shown only to the users assigned to them.
    store = get_store()
    return store.get_policy(instrument_uuid) != "restricted" or store.can_download(user, instrument_uuid)


def station_is_controllable(user, instrument_uuid: str):
    return get_store().can_control_station(user, instrument_uuid)


def logo_url(path: str):
    if not path:
        return None
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    return url_for("public.asset_file", kind="app", name=p.name)


def station_logo_dir() -> Path:
    out = Path(storage_root_or_404()) / "_logos"
    out.mkdir(parents=True, exist_ok=True)
    return out

"""State and access checks shared by the route modules."""

import mimetypes
from pathlib import Path

from flask import abort, redirect, request, send_file, session, url_for

from sensor_network_pwa.access_store import AccessStore
from sensor_network_pwa.storage import collect_instruments


class WebContext:
    """Configuration, auth store and the access helpers every route module uses."""

    def __init__(self, cfg: dict, access_store: AccessStore):
        self.cfg = cfg
        self.access_store = access_store
        self.app_logo_path = str(cfg.get("web_app_logo") or "").strip()

    @staticmethod
    def send_image_asset(path: Path):
        mime, _ = mimetypes.guess_type(str(path))
        response = send_file(path, mimetype=mime or "application/octet-stream")
        # Uploaded SVG files may carry scripts; never let them run in this origin.
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return response

    @staticmethod
    def current_request_target() -> str:
        target = request.full_path or request.path or url_for("index")
        return target[:-1] if target.endswith("?") else target

    def redirect_to_login(self, message: str):
        return redirect(url_for("login", next=self.current_request_target(), err=message))

    def current_user(self):
        username = session.get("username")
        if not username:
            return None
        user = self.access_store.get_user(username)
        return user if user and int(user.get("active") or 0) == 1 else None

    def require_admin(self):
        user = self.current_user()
        if not user:
            return self.redirect_to_login("Please log in to access this page.")
        if user.get("role") != "admin":
            abort(403)
        return user

    def require_login(self):
        user = self.current_user()
        if not user:
            return self.redirect_to_login("Please log in to continue.")
        return user

    def storage_root_or_404(self):
        storage_root = self.cfg.get("storage_root")
        if not storage_root:
            abort(404, "Storage is not enabled/configured")
        return storage_root

    @staticmethod
    def available_instruments(storage_root: str):
        return collect_instruments(storage_root)

    def station_is_accessible(self, user, instrument_uuid: str):
        return self.access_store.can_download(user, instrument_uuid)

    def station_is_public(self, user, instrument_uuid: str):
        # Restricted stations are shown only to the users assigned to them.
        return self.access_store.get_policy(instrument_uuid) != "restricted" or self.access_store.can_download(
            user, instrument_uuid
        )

    def station_is_controllable(self, user, instrument_uuid: str):
        return self.access_store.can_control_station(user, instrument_uuid)

    @staticmethod
    def logo_url(path: str):
        if not path:
            return None
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None
        return url_for("asset_file", kind="app", name=p.name)

    def station_logo_dir(self):
        root = self.storage_root_or_404()
        out = Path(root) / "_logos"
        out.mkdir(parents=True, exist_ok=True)
        return out

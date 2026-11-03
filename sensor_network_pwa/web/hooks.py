"""Request hooks: cross-site write rejection, enforced password change, response headers and compression."""

import gzip
import hmac
import secrets
from urllib.parse import urlsplit

from flask import abort, g, redirect, request, session, url_for

from sensor_network_pwa.log import logger
from sensor_network_pwa.timeutil import format_datetime
from sensor_network_pwa.web.access import current_user, get_cfg, get_store

COMPRESSIBLE_MIMETYPES = frozenset(
    {
        "text/html",
        "text/css",
        "text/javascript",
        "application/javascript",  # what Python 3.10 and 3.11 call .js files
        "application/json",
        "application/manifest+json",
    }
)

# Reachable while a password change is pending: the change itself, logging out, and assets.
PASSWORD_CHANGE_EXEMPT_ENDPOINTS = frozenset(
    {
        "auth.change_password",
        "auth.logout",
        "public.asset_file",
        "static",
        "pwa.manifest",
        "pwa.service_worker",
        "pwa.icon",
        "pwa.offline",
    }
)

CACHEABLE_ASSET_ENDPOINTS = frozenset(
    {
        "public.asset_file",
        "public.documentation_asset",
        "static",
        "pwa.manifest",
        "pwa.service_worker",
        "pwa.icon",
        "pwa.offline",
    }
)

AUDITED_ENDPOINTS = {
    "public.station": "station_dashboard",
    "stations.browse_station": "station_view",
    "stations.export_station_csv": "csv_download",
    "stations.download": "raw_download",
    "api.query": "api_query",
    "api.download": "api_download",
}


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def protect_against_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return None
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    expected = session.get("csrf_token") or ""
    if not supplied or not expected or not hmac.compare_digest(str(supplied), str(expected)):
        abort(403, "Invalid or missing CSRF token")
    return None


def reject_cross_site_writes():
    # CSRF defence for the form endpoints: a browser-supplied Origin/Referer
    # naming another host is refused. Ports and schemes are ignored because
    # reverse proxies commonly rewrite them.
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    source = request.headers.get("Origin") or request.headers.get("Referer") or ""
    try:
        source_host = (urlsplit(source).hostname or "").lower()
    except ValueError:
        source_host = ""
    if not source_host:
        return None
    allowed = set()
    for candidate in (
        request.host,
        request.headers.get("X-Forwarded-Host", "").split(",")[0].strip(),
        urlsplit(get_cfg().get("base_url") or "").netloc,
    ):
        try:
            host = (urlsplit("//" + candidate).hostname or "").lower() if candidate else ""
        except ValueError:
            host = ""
        if host:
            allowed.add(host)
    if source_host not in allowed:
        logger.warning("Rejected cross-site %s %s from origin host %s", request.method, request.path, source_host)
        abort(403, "Cross-site request rejected")
    return None


def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    # Cross-origin map providers require the requesting site's origin for abuse control.
    # This policy sends only the origin over HTTPS, without leaking URL paths or queries.
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; img-src 'self' data: https:; "
        "connect-src 'self'; font-src 'self' https://cdn.jsdelivr.net; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
    )
    if urlsplit(get_cfg().get("base_url") or "").scheme.lower() == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    # Authenticated pages and bearer-token responses may contain private station or
    # account data. Do not leave them in a browser or intermediary cache. Static and
    # generated PWA assets retain their route-specific caching policy.
    if request.endpoint not in CACHEABLE_ASSET_ENDPOINTS and (
        session.get("username") or request.headers.get("Authorization")
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


def compress_response(response):
    static_file = response.direct_passthrough and request.endpoint == "static"
    if (
        (response.direct_passthrough and not static_file)
        or response.status_code != 200
        or response.headers.get("Content-Encoding")
        or response.mimetype not in COMPRESSIBLE_MIMETYPES
        or "gzip" not in request.headers.get("Accept-Encoding", "").lower()
    ):
        return response
    if static_file:
        # Page scripts and stylesheets are small files: read them so that they are compressed too.
        stream = response.response
        response.direct_passthrough = False
        data = response.get_data()
        response.set_data(data)
        if hasattr(stream, "close"):
            stream.close()
    else:
        data = response.get_data()
    if len(data) < 1024:
        return response
    response.set_data(gzip.compress(data, compresslevel=5))
    response.headers["Content-Encoding"] = "gzip"
    response.vary.add("Accept-Encoding")
    return response


def enforce_password_change():
    if request.endpoint in PASSWORD_CHANGE_EXEMPT_ENDPOINTS:
        return None
    user = current_user()
    if user and not session.get("impersonator_username") and int(user.get("force_password_change") or 0) == 1:
        if request.method not in ("GET", "HEAD") or request.path.startswith("/api/"):
            abort(403, "Password change required")
        return redirect(url_for("auth.change_password"))
    return None


def record_access_event(response):
    """Record user-visible data access without logging static assets or polling routes."""
    endpoint = request.endpoint or ""
    event_type = AUDITED_ENDPOINTS.get(endpoint)
    if endpoint == "auth.login" and request.method == "POST":
        event_type = "login_success" if response.status_code in (301, 302, 303) else "login_failure"
    elif endpoint == "auth.fast_login" and response.status_code in (301, 302, 303):
        event_type = "login_success"
    elif endpoint == "auth.logout":
        event_type = "logout"
    if not event_type:
        return response

    user = getattr(g, "user", None) or current_user()
    username = str(getattr(g, "audit_username", "") or (user or {}).get("username") or "") or None
    if event_type == "login_failure":
        username = str(request.form.get("username", "")).strip() or None
    station = str((request.view_args or {}).get("instrument_uuid") or request.args.get("station") or "").strip()
    try:
        get_store().record_access_event(
            event_type,
            username=username,
            station_uuid=station or None,
            method=request.method,
            path=request.path,
            status_code=response.status_code,
        )
    except Exception as error:  # Audit logging must never make the portal unavailable.
        logger.error("Could not record access event endpoint=%s err=%s", endpoint, error)
    return response


def init_app(app):
    # before_request hooks run in registration order; after_request hooks in reverse.
    app.before_request(reject_cross_site_writes)
    app.before_request(protect_against_csrf)
    app.before_request(enforce_password_change)
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.filters["datetime_display"] = format_datetime
    app.after_request(add_security_headers)
    app.after_request(compress_response)
    app.after_request(record_access_event)

"""Request hooks: cross-site write rejection, enforced password change, response headers and compression."""

import gzip
from urllib.parse import urlsplit

from flask import abort, redirect, request, url_for

from sensor_network_pwa.log import logger
from sensor_network_pwa.web.access import current_user, get_cfg

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
    response.headers.setdefault("Referrer-Policy", "same-origin")
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
    if user and int(user.get("force_password_change") or 0) == 1:
        if request.method not in ("GET", "HEAD") or request.path.startswith("/api/"):
            abort(403, "Password change required")
        return redirect(url_for("auth.change_password"))
    return None


def init_app(app):
    # before_request hooks run in registration order; after_request hooks in reverse.
    app.before_request(reject_cross_site_writes)
    app.before_request(enforce_password_change)
    app.after_request(add_security_headers)
    app.after_request(compress_response)

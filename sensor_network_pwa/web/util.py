"""Small helpers used by the page routes."""

import json


def json_for_script(value) -> str:
    """Serialize JSON for embedding inside an HTML <script> element."""
    return (
        json.dumps(value, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )


def request_preference_cookie(request_obj, param_name: str, cookie_name: str, normalizer, default: str):
    raw = request_obj.args.get(param_name)
    if raw is not None and str(raw).strip():
        return normalizer(raw)
    cookie_val = request_obj.cookies.get(cookie_name, "")
    if cookie_val:
        return normalizer(cookie_val)
    return default

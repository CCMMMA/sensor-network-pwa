"""Validation of user-supplied names, addresses and passwords."""

import re


def safe_filename(name: str):
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip())
    return base[:180] or "file"


def is_valid_email(value: str) -> bool:
    return len(value) <= 254 and re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", value) is not None


def validate_password_strength(password: str):
    if not isinstance(password, str) or not password:
        return False, "Password is required"
    if len(password) < 12:
        return False, "Password must be at least 12 characters long"
    checks = [
        (r"[A-Z]", "one uppercase letter"),
        (r"[a-z]", "one lowercase letter"),
        (r"[0-9]", "one digit"),
        (r"[^A-Za-z0-9]", "one special character"),
    ]
    missing = [label for pattern, label in checks if re.search(pattern, password) is None]
    if missing:
        return False, "Password must include " + ", ".join(missing)
    return True, ""

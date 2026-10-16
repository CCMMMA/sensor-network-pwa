"""Outgoing email and the absolute links placed in it."""

import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode

from sensor_network_pwa.log import logger


def send_email(cfg: dict, recipients, subject: str, body_text: str):
    recipients = [r.strip() for r in (recipients or []) if isinstance(r, str) and r.strip()]
    if not recipients:
        return False
    if not cfg.get("smtp_enabled"):
        logger.info("SMTP disabled; skipped email subject=%s recipients=%d", subject, len(recipients))
        return False
    if not str(cfg.get("smtp_host", "") or "").strip():
        logger.info("SMTP host not configured; skipped email subject=%s recipients=%d", subject, len(recipients))
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = " ".join(str(subject).split())
        msg["From"] = cfg.get("smtp_from", "")
        msg["To"] = ", ".join(recipients)
        msg.set_content(body_text)

        smtp_host = cfg.get("smtp_host", "")
        smtp_port = int(cfg.get("smtp_port", 25))
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
            if cfg.get("smtp_use_tls", True):
                smtp.starttls()
            if cfg.get("smtp_user"):
                smtp.login(cfg.get("smtp_user", ""), cfg.get("smtp_pass", ""))
            smtp.send_message(msg)
        logger.info("Email sent subject=%s recipients=%d", subject, len(recipients))
        return True
    except Exception as e:
        logger.warning("Email send failed subject=%s recipients=%d err=%s", subject, len(recipients), e)
        return False


def compose_external_url(base_url: str, path: str, query=None) -> str:
    root = str(base_url or "").strip().rstrip("/")
    suffix = "/" + str(path or "").lstrip("/")
    out = f"{root}{suffix}" if root else suffix
    if query:
        return f"{out}?{urlencode(query)}"
    return out

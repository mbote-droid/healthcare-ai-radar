"""SMTP email delivery for the digest.

Standard library only (smtplib) - no extra dependency. If credentials are not
configured, ``email_config_from_settings`` returns None and the caller simply
skips emailing; a send that fails at the network/auth layer is logged and
returns False rather than raising. Secrets are never logged.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from config import settings
from radar.logconf import log


@dataclass
class EmailConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipient: str


def email_config_from_settings() -> EmailConfig | None:
    """Build config from settings, or None if username/password are missing."""
    if not settings.EMAIL_USERNAME or not settings.EMAIL_PASSWORD:
        return None
    if not settings.EMAIL_TO:
        return None
    return EmailConfig(
        host=settings.EMAIL_HOST,
        port=settings.EMAIL_PORT,
        username=settings.EMAIL_USERNAME,
        password=settings.EMAIL_PASSWORD,
        sender=settings.EMAIL_FROM,
        recipient=settings.EMAIL_TO,
    )


def send_email(
    subject: str, html_body: str, config: EmailConfig, text_body: str | None = None
) -> bool:
    """Send a multipart (text + HTML) email. Returns True on success."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.sender
    msg["To"] = config.recipient
    msg.set_content(text_body or "This digest is best viewed in an HTML client.")
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(config.host, config.port, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(config.username, config.password)
            server.send_message(msg)
        log.info(f"email sent to {config.recipient}")
        return True
    except Exception as exc:  # smtplib raises a family of exceptions
        # Never interpolate the password; log only the exception type/message.
        log.warning(f"email send failed ({type(exc).__name__}): {exc}")
        return False

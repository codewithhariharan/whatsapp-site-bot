"""Sending email: SMTP with STARTTLS, nothing else.

Only /tunnel sends mail. The standard library is enough — one message at a
time, on request — so there is no mail SDK to install or keep current.
"""

import smtplib
from email.message import EmailMessage

from config import settings


class EmailNotConfigured(RuntimeError):
    """SMTP_HOST or the recipients are unset."""


def is_configured(recipients: list[str]) -> bool:
    # A login with no password is refused at send time; say "not set up"
    # up front instead of "didn't send" after the summary is built.
    login_ready = not settings.SMTP_USER or bool(settings.SMTP_PASSWORD)
    return bool(settings.SMTP_HOST and recipients and login_ready)


def send_email(recipients: list[str], subject: str, text: str,
               html: str | None = None) -> None:
    """Send one message. Blocking: callers on the event loop use a thread.

    `text` is the plain body every client can show; `html`, when given, is the
    formatted alternative most clients prefer.
    """
    if not is_configured(recipients):
        raise EmailNotConfigured("SMTP_HOST and a recipient are required")

    sender = settings.EMAIL_FROM or settings.SMTP_USER
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        if settings.SMTP_USER:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(msg)

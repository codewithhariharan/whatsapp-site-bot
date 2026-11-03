"""Sending email: SMTP with STARTTLS, nothing else.

/tunnel and the 6-hourly record workbook (record_export.py) send mail. The standard library is enough — one message at a
time, on request — so there is no mail SDK to install or keep current.
"""

import smtplib
from email.message import EmailMessage

import sandbox
from config import settings


class EmailNotConfigured(RuntimeError):
    """SMTP_HOST or the recipients are unset."""


def is_configured(recipients: list[str]) -> bool:
    # A login with no password is refused at send time; say "not set up"
    # up front instead of "didn't send" after the summary is built.
    login_ready = not settings.SMTP_USER or bool(settings.SMTP_PASSWORD)
    return bool(settings.SMTP_HOST and recipients and login_ready)


def send_email(recipients: list[str], subject: str, text: str,
               html: str | None = None,
               attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    """Send one message. Blocking: callers on the event loop use a thread.

    `text` is the plain body every client can show; `html`, when given, is the
    formatted alternative most clients prefer. `attachments` are
    (filename, bytes, "maintype/subtype").
    """
    if not is_configured(recipients):
        raise EmailNotConfigured("SMTP_HOST and a recipient are required")
    # The dashboard's test console shows the email instead of sending it.
    if sandbox.capture({"type": "email", "to": recipients, "subject": subject,
                        "text": text, "html": html,
                        "attachments": [name for name, _, _ in attachments or []]}):
        return

    sender = settings.EMAIL_FROM or settings.SMTP_USER
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, data, mimetype in attachments or []:
        maintype, _, subtype = mimetype.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        if settings.SMTP_USER:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(msg)

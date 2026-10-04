"""Системные письма платформы. Без SMTP-настроек письма пишутся в лог (dev)."""
import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

log = logging.getLogger(__name__)


def send_mail(to: str, subject: str, body: str) -> bool:
    cfg = current_app.config
    if not cfg["SMTP_HOST"]:
        log.warning("SMTP не настроен. Письмо для %s:\nSubject: %s\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as smtp:
            if cfg["SMTP_STARTTLS"]:
                smtp.starttls()
            if cfg["SMTP_USER"]:
                smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            smtp.send_message(msg)
        return True
    except Exception:
        log.exception("Не удалось отправить письмо")
        return False

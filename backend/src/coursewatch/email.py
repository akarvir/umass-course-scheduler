import smtplib
from email.message import EmailMessage

import httpx

from coursewatch.config import settings


def send_email(recipient: str, subject: str, body: str, delivery_id: int):
    config = settings()
    if config.email_backend == "resend":
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {config.resend_api_key}",
                "Idempotency-Key": f"coursewatch-delivery-{delivery_id}",
            },
            json={"from": config.email_from, "to": [recipient], "subject": subject, "text": body},
            timeout=30,
        )
        response.raise_for_status()
        return
    message = EmailMessage()
    message["From"] = config.email_from
    message["To"] = recipient
    message["Subject"] = subject
    message["Message-ID"] = f"<coursewatch-{delivery_id}@coursewatch.local>"
    message.set_content(body)
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20) as smtp:
        if config.smtp_starttls:
            smtp.starttls()
        if config.smtp_user:
            smtp.login(config.smtp_user, config.smtp_password)
        smtp.send_message(message)

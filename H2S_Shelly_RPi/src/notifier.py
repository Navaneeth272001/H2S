import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from .config import SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO

logger = logging.getLogger(__name__)

async def send_email(subject: str, message: str):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        logger.warning("Email credentials not configured. Skipping email alert.")
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = EMAIL_TO
        msg["Subject"] = subject
        msg.attach(MIMEText(message, "plain", "utf-8"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
            logger.info(f"Alert email sent: {subject}")

    except Exception as e:
        logger.error(f"Failed to send email alert: {e}")

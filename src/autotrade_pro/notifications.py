"""Email and SMS confirmation delivery."""

from __future__ import annotations

from email.message import EmailMessage
import smtplib
from typing import Any

import requests

from .config import AppConfig


def send_confirmation(config: AppConfig, valuation: dict[str, Any]) -> list[str]:
    """Send best-effort appointment confirmations.

    Delivery failures are returned as notes instead of breaking the booking flow.
    """

    notes: list[str] = []
    customer_email = valuation.get("customer_email") or ""
    customer_phone = valuation.get("customer_phone") or ""
    if customer_email:
        error = _send_email(config, customer_email, valuation)
        if error:
            notes.append(error)
    if customer_phone and config.sms_webhook_url:
        error = _send_sms(config, customer_phone, valuation)
        if error:
            notes.append(error)
    return notes


def _send_email(config: AppConfig, recipient: str, valuation: dict[str, Any]) -> str | None:
    if not config.smtp_host or not config.smtp_username or not config.smtp_password:
        return "SMTP is not configured; confirmation email was not sent."

    dealer_address = ", ".join(
        part
        for part in [
            valuation.get("address_line1"),
            valuation.get("city"),
            valuation.get("state"),
            valuation.get("postal_code"),
        ]
        if part
    )
    message = EmailMessage()
    message["Subject"] = f"Your AutoTrade Pro appointment {valuation.get('confirmation_code')}"
    message["From"] = config.smtp_username
    message["To"] = recipient
    message.set_content(
        "\n".join(
            [
                f"Trade value: ${valuation.get('trade_offer', 0):,}",
                f"Vehicle: {valuation.get('year') or ''} {valuation.get('make') or ''} {valuation.get('model') or ''}".strip(),
                f"Appointment: {valuation.get('scheduled_date')} at {valuation.get('scheduled_time')}",
                f"Dealer: {valuation.get('dealer_name')}",
                f"Address: {dealer_address}",
                f"Confirmation: {valuation.get('confirmation_code')}",
                "",
                "Offer is subject to in-person verification and title review.",
            ]
        )
    )
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=12) as smtp:
            smtp.starttls()
            smtp.login(config.smtp_username, config.smtp_password)
            smtp.send_message(message)
    except Exception as exc:
        return f"Email delivery failed: {exc}"
    return None


def _send_sms(config: AppConfig, recipient: str, valuation: dict[str, Any]) -> str | None:
    try:
        response = requests.post(
            config.sms_webhook_url,
            json={
                "to": recipient,
                "message": (
                    f"AutoTrade Pro appointment {valuation.get('confirmation_code')}: "
                    f"${valuation.get('trade_offer', 0):,} estimate, "
                    f"{valuation.get('scheduled_date')} {valuation.get('scheduled_time')}."
                ),
            },
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:
        return f"SMS delivery failed: {exc}"
    return None

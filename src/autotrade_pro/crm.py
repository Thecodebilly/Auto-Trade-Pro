"""CRM delivery helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from .config import AppConfig
from .database import add_crm_event


def emit_crm_event(
    db_path: Path,
    config: AppConfig,
    dealer: dict[str, Any],
    valuation_id: int | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    webhook_url = dealer.get("crm_webhook_url") or config.crm_webhook_url
    if not webhook_url:
        add_crm_event(
            db_path,
            dealer_id=dealer["id"],
            valuation_id=valuation_id,
            event_type=event_type,
            payload=payload,
            status="queued_no_webhook",
            response_text="No CRM webhook configured for this dealer.",
        )
        return

    try:
        response = requests.post(
            webhook_url,
            json={"event": event_type, "dealer": dealer.get("slug"), "payload": payload},
            timeout=10,
            headers={"User-Agent": "AutoTradePro/1.0"},
        )
        response.raise_for_status()
    except Exception as exc:
        add_crm_event(
            db_path,
            dealer_id=dealer["id"],
            valuation_id=valuation_id,
            event_type=event_type,
            payload=payload,
            status="failed",
            response_text=str(exc),
        )
        return

    add_crm_event(
        db_path,
        dealer_id=dealer["id"],
        valuation_id=valuation_id,
        event_type=event_type,
        payload=payload,
        status="delivered",
        response_text=response.text[:1000],
    )

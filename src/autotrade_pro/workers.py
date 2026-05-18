"""Background and CLI market-data workers."""

from __future__ import annotations

import logging
from pathlib import Path
import threading
import time

from flask import Flask

from .config import AppConfig
from .market_data import import_market_csv


LOGGER = logging.getLogger(__name__)


def refresh_market_data_once(config: AppConfig) -> dict[str, int | str]:
    """Refresh dealer-owned market snapshots from configured sources."""

    imported = 0
    if config.market_feed_csv:
        csv_path = Path(config.market_feed_csv).expanduser().resolve(strict=False)
        if csv_path.exists():
            imported = import_market_csv(config.database_path, csv_path, config.market_region)
        else:
            return {"status": "missing_csv", "imported": 0, "path": str(csv_path)}
    return {"status": "ok", "imported": imported}


def start_market_refresh_worker(app: Flask, config: AppConfig) -> None:
    """Start a lightweight thread that periodically ingests configured feeds."""

    def run() -> None:
        with app.app_context():
            while True:
                try:
                    result = refresh_market_data_once(config)
                    LOGGER.info("Market refresh completed: %s", result)
                except Exception:
                    LOGGER.exception("Market refresh failed")
                time.sleep(max(60, config.worker_interval_seconds))

    thread = threading.Thread(target=run, name="market-refresh-worker", daemon=True)
    thread.start()

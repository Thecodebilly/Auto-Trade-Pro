"""Runtime configuration for AutoTrade Pro."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class AppConfig:
    """Container for all app-level settings."""

    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("AUTOTRADE_DATA_DIR", "data"))
    )
    database_filename: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_DB_FILE", "autotrade.db")
    )
    upload_folder_name: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_UPLOAD_DIR", "vehicle_uploads")
    )
    secret_key: str = field(
        default_factory=lambda: os.getenv(
            "AUTOTRADE_SECRET_KEY", "dev-autotrade-pro-change-me"
        )
    )
    admin_password: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_ADMIN_PASSWORD", "AutoTrade123!")
    )
    default_dealer_slug: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_DEFAULT_DEALER", "south-florida-demo")
    )
    nhtsa_api_base: str = field(
        default_factory=lambda: os.getenv(
            "AUTOTRADE_NHTSA_API_BASE", "https://vpic.nhtsa.dot.gov/api"
        ).rstrip("/")
    )
    nhtsa_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("AUTOTRADE_NHTSA_TIMEOUT_SECONDS", "8"))
    )
    market_region: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_MARKET_REGION", "south_florida")
    )
    market_feed_csv: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_MARKET_FEED_CSV", "")
    )
    manheim_api_base: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_MANHEIM_API_BASE", "").rstrip("/")
    )
    manheim_api_key: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_MANHEIM_API_KEY", "")
    )
    jd_power_api_base: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_JD_POWER_API_BASE", "").rstrip("/")
    )
    jd_power_api_key: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_JD_POWER_API_KEY", "")
    )
    black_book_api_base: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_BLACK_BOOK_API_BASE", "").rstrip("/")
    )
    black_book_api_key: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_BLACK_BOOK_API_KEY", "")
    )
    crm_webhook_url: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_CRM_WEBHOOK_URL", "")
    )
    smtp_host: str = field(default_factory=lambda: os.getenv("AUTOTRADE_SMTP_HOST", ""))
    smtp_port: int = field(
        default_factory=lambda: int(os.getenv("AUTOTRADE_SMTP_PORT", "587"))
    )
    smtp_username: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_SMTP_USERNAME", "")
    )
    smtp_password: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_SMTP_PASSWORD", "")
    )
    sms_webhook_url: str = field(
        default_factory=lambda: os.getenv("AUTOTRADE_SMS_WEBHOOK_URL", "")
    )
    enable_worker: bool = field(
        default_factory=lambda: _env_bool("AUTOTRADE_ENABLE_WORKER", False)
    )
    worker_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("AUTOTRADE_WORKER_INTERVAL_SECONDS", "3600"))
    )
    database_path: Path = field(init=False)
    uploads_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser().resolve(strict=False)
        self.database_path = self.data_dir / self.database_filename
        self.uploads_path = self.data_dir / self.upload_folder_name

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_path.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    config = AppConfig()
    config.ensure_directories()
    return config

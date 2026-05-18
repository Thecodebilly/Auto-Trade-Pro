"""AutoTrade Pro Flask application factory."""

from __future__ import annotations

from flask import Flask

from .config import AppConfig, load_config
from .database import init_db, seed_demo_data
from .routes import create_blueprint
from .workers import start_market_refresh_worker


def create_app(config: AppConfig | None = None) -> Flask:
    config = config or load_config()
    init_db(config.database_path)
    seed_demo_data(config.database_path, default_slug=config.default_dealer_slug)

    app = Flask(__name__, template_folder="templates")
    app.config["AUTOTRADE_CONFIG"] = config
    app.secret_key = config.secret_key
    app.register_blueprint(create_blueprint(config))

    if config.enable_worker:
        start_market_refresh_worker(app, config)

    return app


__all__ = ["AppConfig", "create_app"]

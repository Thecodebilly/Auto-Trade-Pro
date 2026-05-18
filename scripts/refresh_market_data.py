"""Run a one-shot market data refresh."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from autotrade_pro.config import load_config  # noqa: E402
from autotrade_pro.database import init_db, seed_demo_data  # noqa: E402
from autotrade_pro.workers import refresh_market_data_once  # noqa: E402


def main() -> int:
    config = load_config()
    init_db(config.database_path)
    seed_demo_data(config.database_path, default_slug=config.default_dealer_slug)
    result = refresh_market_data_once(config)
    print(result)
    return 0 if result.get("status") in {"ok", "missing_csv"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

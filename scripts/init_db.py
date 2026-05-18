"""Initialize or migrate the AutoTrade Pro database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from autotrade_pro.config import load_config  # noqa: E402
from autotrade_pro.database import connect, init_db, seed_demo_data  # noqa: E402
from autotrade_pro.workers import refresh_market_data_once  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create/migrate the SQLite database and seed AutoTrade Pro demo data."
    )
    parser.add_argument(
        "--refresh-market",
        action="store_true",
        help="Import AUTOTRADE_MARKET_FEED_CSV after schema initialization.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable initialization details.",
    )
    args = parser.parse_args(argv)

    config = load_config()
    init_db(config.database_path)
    seed_demo_data(config.database_path, default_slug=config.default_dealer_slug)
    market_refresh = refresh_market_data_once(config) if args.refresh_market else None
    summary = _database_summary(config.database_path)
    summary.update(
        {
            "database_path": str(config.database_path),
            "uploads_path": str(config.uploads_path),
            "default_dealer": config.default_dealer_slug,
            "market_refresh": market_refresh,
        }
    )

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Initialized AutoTrade Pro database: {config.database_path}")
        print(f"Default dealer: {config.default_dealer_slug}")
        print(f"Dealers: {summary['dealers']}")
        print(f"Incentives: {summary['incentives']}")
        print(f"Market snapshots: {summary['market_snapshots']}")
        if market_refresh:
            print(
                f"Market refresh: {market_refresh.get('status')} "
                f"({market_refresh.get('imported', 0)} imported)"
            )
    return 0


def _database_summary(db_path: Path) -> dict[str, int]:
    tables = ["dealers", "incentives", "market_snapshots", "valuations"]
    with connect(db_path) as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


if __name__ == "__main__":
    raise SystemExit(main())

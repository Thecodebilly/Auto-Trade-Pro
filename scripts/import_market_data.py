#!/usr/bin/env python3
"""Bulk import production market valuation snapshots from CSV."""

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
from autotrade_pro.database import connect, database_backend, init_db, seed_demo_data  # noqa: E402
from autotrade_pro.market_data import import_market_csv  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import a large market valuation CSV into AutoTrade Pro."
    )
    parser.add_argument("csv_path", help="Path to the market valuation CSV.")
    parser.add_argument(
        "--region",
        default="",
        help="Default region for rows without a region column value.",
    )
    parser.add_argument(
        "--source",
        default="",
        help="Override the source name for every row, e.g. kelley_blue_book_production.",
    )
    parser.add_argument(
        "--replace-source",
        action="store_true",
        help="Delete existing rows for the imported source/region before inserting.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args(argv)

    csv_path = Path(args.csv_path).expanduser().resolve(strict=False)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 1

    config = load_config()
    init_db(config.database_path)
    seed_demo_data(config.database_path, default_slug=config.default_dealer_slug)
    region = args.region or config.market_region
    imported = import_market_csv(
        config.database_path,
        csv_path,
        region,
        source_override=args.source,
        replace_source=args.replace_source,
    )
    summary = _source_summary(config.database_path)
    backend = database_backend()
    payload = {
        "status": "ok",
        "database_backend": backend,
        "database_path": str(config.database_path),
        "csv_path": str(csv_path),
        "imported": imported,
        "source_summary": summary,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Imported {imported:,} market rows using {backend}")
        if backend == "sqlite":
            print(f"SQLite file: {config.database_path}")
        for row in summary:
            print(
                f"- {row['source']} / {row['region']}: "
                f"{row['rows_count']:,} rows"
            )
    return 0


def _source_summary(db_path: Path) -> list[dict[str, object]]:
    with connect(db_path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT source, region, COUNT(*) AS rows_count,
                       MAX(captured_at) AS latest_capture
                FROM market_snapshots
                GROUP BY source, region
                ORDER BY rows_count DESC, source
                """
            ).fetchall()
        ]


if __name__ == "__main__":
    raise SystemExit(main())

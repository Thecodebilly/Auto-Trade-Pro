#!/usr/bin/env python3
"""Build the 5,000-vehicle real-data trade-in test fixture.

Vehicle identities and configuration data come from the public EPA
FuelEconomy.gov vehicles.csv file. The fixture intentionally stores the
sample in-repo so normal test runs do not require network access.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import zipfile

import requests


COUNT = 5_000
SOURCE_NAME = "EPA FuelEconomy.gov vehicles.csv"
SOURCE_URL = "https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip"
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "tests" / "fixtures" / "real_trade_in_vehicles.json"


def main() -> int:
    rows = _download_vehicle_rows()
    selected = _deterministic_even_sample(rows, COUNT)
    vehicles = [_vehicle_record(index, row) for index, row in enumerate(selected)]
    payload = {
        "metadata": {
            "source": SOURCE_NAME,
            "source_url": SOURCE_URL,
            "selection": (
                "Deterministic evenly spaced sample over EPA rows sorted by "
                "year, make, model, and source id."
            ),
            "vehicle_identity_note": (
                "Vehicle year/make/model/configuration fields are real EPA public data."
            ),
            "value_note": (
                "Trade-in market values in tests are deterministic valuation scenarios "
                "derived around these real vehicle identities, not EPA-provided prices."
            ),
            "count": COUNT,
            "upstream_row_count": len(rows),
        },
        "vehicles": vehicles,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(vehicles):,} real vehicle rows to {OUTPUT_PATH}")
    return 0


def _download_vehicle_rows() -> list[dict[str, str]]:
    response = requests.get(SOURCE_URL, timeout=45)
    response.raise_for_status()

    rows: list[dict[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        with archive.open("vehicles.csv") as raw_file:
            reader = csv.DictReader(io.TextIOWrapper(raw_file, encoding="latin-1"))
            for row in reader:
                if not row.get("id") or not row.get("year") or not row.get("make"):
                    continue
                if not row.get("model"):
                    continue
                try:
                    row["_year_sort"] = str(int(row["year"]))
                    row["_id_sort"] = str(int(row["id"]))
                except ValueError:
                    continue
                rows.append(row)

    if len(rows) < COUNT:
        raise RuntimeError(f"EPA dataset only produced {len(rows):,} usable rows")

    rows.sort(
        key=lambda row: (
            int(row["_year_sort"]),
            _clean_text(row.get("make")),
            _clean_text(row.get("model")),
            int(row["_id_sort"]),
        )
    )
    return rows


def _deterministic_even_sample(
    rows: list[dict[str, str]], count: int
) -> list[dict[str, str]]:
    last_index = len(rows) - 1
    selected = [rows[round(index * last_index / (count - 1))] for index in range(count)]
    source_ids = [row["id"] for row in selected]
    if len(set(source_ids)) != count:
        raise RuntimeError("Deterministic sample produced duplicate EPA vehicle ids")
    return selected


def _vehicle_record(index: int, row: dict[str, str]) -> dict[str, object]:
    return {
        "fixture_index": index,
        "source": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "source_id": _clean_text(row.get("id")),
        "year": _int(row.get("year")),
        "make": _clean_text(row.get("make")),
        "model": _clean_text(row.get("model")),
        "trim": _configuration_label(row),
        "body_style": _clean_text(row.get("VClass")),
        "drive": _clean_text(row.get("drive")),
        "transmission": _clean_text(row.get("trany")),
        "fuel_type": _clean_text(row.get("fuelType")),
        "cylinders": _int(row.get("cylinders")),
        "displacement_liters": _float(row.get("displ")),
        "city_mpg": _int(row.get("city08")),
        "highway_mpg": _int(row.get("highway08")),
        "combined_mpg": _int(row.get("comb08")),
    }


def _configuration_label(row: dict[str, str]) -> str:
    engine_parts = []
    displacement = _clean_text(row.get("displ"))
    cylinders = _clean_text(row.get("cylinders"))
    if displacement:
        engine_parts.append(f"{displacement}L")
    if cylinders:
        engine_parts.append(f"{cylinders} cyl")

    parts = [
        _clean_text(row.get("drive")),
        _clean_text(row.get("trany")),
        " ".join(engine_parts),
        _clean_text(row.get("fuelType")),
    ]
    return ", ".join(part for part in parts if part) or "EPA listed configuration"


def _clean_text(value: object | None) -> str:
    return " ".join(str(value or "").split())


def _int(value: object | None) -> int | None:
    value = _clean_text(value)
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _float(value: object | None) -> float | None:
    value = _clean_text(value)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())

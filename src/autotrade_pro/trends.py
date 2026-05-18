"""Trade-in value trend projections."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


NORMAL_ANNUAL_MILES = 12_000


def build_trade_value_trend(
    valuation: dict[str, Any], years: int = 5
) -> dict[str, Any]:
    """Project trade-in value under normal future usage.

    The projection is anchored to the actual calculated trade offer. It then
    applies age-sensitive retention assumptions so newer vehicles depreciate
    faster while older vehicles flatten out.
    """

    years = max(1, min(int(years), 5))
    current_value = _round_to_50(_float(valuation.get("trade_offer")))
    current_mileage = max(0, int(_float(valuation.get("mileage"))))
    vehicle_year = _int_or_none(valuation.get("year"))
    current_year = datetime.now(timezone.utc).year
    current_age = max(0, current_year - vehicle_year) if vehicle_year else 5
    make = str(valuation.get("make") or "").upper()
    body_style = str(valuation.get("body_style") or "").upper()

    points = [
        {
            "year_offset": 0,
            "label": "Now",
            "vehicle_age": current_age,
            "mileage": current_mileage,
            "trade_value": current_value,
            "projected": False,
        }
    ]

    projected_value = current_value
    for year_offset in range(1, years + 1):
        projected_age = current_age + year_offset
        projected_mileage = current_mileage + NORMAL_ANNUAL_MILES * year_offset
        retention = _annual_retention(projected_age, make, body_style)
        projected_value = max(500, _round_to_50(projected_value * retention))
        points.append(
            {
                "year_offset": year_offset,
                "label": f"+{year_offset} yr",
                "vehicle_age": projected_age,
                "mileage": projected_mileage,
                "trade_value": min(projected_value, points[-1]["trade_value"]),
                "projected": True,
                "retention_rate": retention,
            }
        )

    return {
        "normal_annual_miles": NORMAL_ANNUAL_MILES,
        "projection_years": years,
        "vehicle": {
            "year": vehicle_year,
            "make": valuation.get("make", ""),
            "model": valuation.get("model", ""),
            "trim": valuation.get("trim", ""),
            "body_style": valuation.get("body_style", ""),
        },
        "assumptions": [
            "Projection starts from the current calculated trade offer.",
            f"Normal future usage assumes {NORMAL_ANNUAL_MILES:,} miles per year.",
            "Newer vehicles depreciate faster; older vehicles flatten as they age.",
            "Actual market shifts, accidents, maintenance, demand, and reconditioning can move values up or down.",
        ],
        "points": points,
    }


def _annual_retention(age: int, make: str, body_style: str) -> float:
    if age <= 2:
        retention = 0.83
    elif age <= 5:
        retention = 0.88
    elif age <= 10:
        retention = 0.91
    else:
        retention = 0.94

    text = f"{make} {body_style}"
    if any(term in text for term in {"LEXUS", "TOYOTA", "HONDA", "SUBARU"}):
        retention += 0.015
    if any(term in text for term in {"PICKUP", "TRUCK", "SUV", "CROSSOVER"}):
        retention += 0.01
    if any(term in text for term in {"BMW", "MERCEDES", "AUDI", "PORSCHE", "CADILLAC", "LUXURY"}):
        retention -= 0.02
    if "EV" in text or "TESLA" in text or "ELECTRIC" in text:
        retention -= 0.015 if age <= 5 else 0.005
    if any(term in text for term in {"SPORTS", "CONVERTIBLE", "COUPE"}):
        retention += 0.005
    return max(0.78, min(0.96, retention))


def _round_to_50(value: float) -> int:
    return int(round(value / 50) * 50)


def _float(value: object) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _int_or_none(value: object) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None

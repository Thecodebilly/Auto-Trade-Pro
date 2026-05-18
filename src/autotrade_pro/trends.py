"""Trade-in value trend projections."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


NORMAL_ANNUAL_MILES = 12_000
MAX_TREND_YEARS = 5


def build_trade_value_trend(
    valuation: dict[str, Any], years: int = MAX_TREND_YEARS
) -> dict[str, Any]:
    """Estimate historical and projected trade value around the current offer.

    The curve is anchored to the actual calculated trade offer. Prior points are
    back-cast from that anchor, and future points apply age-, class-, make-, and
    mileage-sensitive depreciation. It is intentionally deterministic so the
    dealer can audit why the chart moved.
    """

    years = max(1, min(int(years), MAX_TREND_YEARS))
    current_value = max(500, _round_to_50(_float(valuation.get("trade_offer"))))
    current_mileage = max(0, int(_float(valuation.get("mileage"))))
    vehicle_year = _int_or_none(valuation.get("year"))
    current_year = datetime.now(timezone.utc).year
    current_age = max(0, current_year - vehicle_year) if vehicle_year else 5
    make = str(valuation.get("make") or "").upper()
    model = str(valuation.get("model") or "").upper()
    body_style = str(valuation.get("body_style") or "").upper()
    history_years = min(years, current_age) if vehicle_year else years

    points_by_offset: dict[int, dict[str, Any]] = {
        0: _point(
            year_offset=0,
            current_year=current_year,
            vehicle_age=current_age,
            mileage=current_mileage,
            trade_value=current_value,
            retention=None,
        )
    }

    historical_value = current_value
    for year_offset in range(-1, -history_years - 1, -1):
        age_after_year = max(1, current_age + year_offset + 1)
        vehicle_age = max(0, current_age + year_offset)
        retention = _annual_retention(age_after_year, make, model, body_style)
        historical_value = _round_to_50(historical_value / max(retention, 0.62))
        points_by_offset[year_offset] = _point(
            year_offset=year_offset,
            current_year=current_year,
            vehicle_age=vehicle_age,
            mileage=max(0, current_mileage + NORMAL_ANNUAL_MILES * year_offset),
            trade_value=max(
                historical_value, points_by_offset[year_offset + 1]["trade_value"]
            ),
            retention=retention,
        )

    projected_value = current_value
    for year_offset in range(1, years + 1):
        projected_age = current_age + year_offset
        projected_mileage = current_mileage + NORMAL_ANNUAL_MILES * year_offset
        retention = _annual_retention(projected_age, make, model, body_style)
        projected_value = max(500, _round_to_50(projected_value * retention))
        points_by_offset[year_offset] = _point(
            year_offset=year_offset,
            current_year=current_year,
            vehicle_age=projected_age,
            mileage=projected_mileage,
            trade_value=min(
                projected_value, points_by_offset[year_offset - 1]["trade_value"]
            ),
            retention=retention,
        )

    points = [points_by_offset[offset] for offset in sorted(points_by_offset)]
    return {
        "normal_annual_miles": NORMAL_ANNUAL_MILES,
        "history_years": history_years,
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
            f"Historical and future mileage assume {NORMAL_ANNUAL_MILES:,} miles per year around today's odometer.",
            "Historical values are estimated back-casts, not stored prior offers.",
            "Newer vehicles, luxury vehicles, and EVs depreciate faster; older vehicles flatten as they age.",
            "Make, model, and body style adjust the curve for stronger or weaker retention.",
            "If AI valuation assist is enabled, its current-offer adjustment is already reflected in the chart anchor.",
            "Actual market shifts, accidents, maintenance, demand, and reconditioning can move values up or down.",
        ],
        "points": points,
    }


def _point(
    *,
    year_offset: int,
    current_year: int,
    vehicle_age: int,
    mileage: int,
    trade_value: int,
    retention: float | None,
) -> dict[str, Any]:
    point = {
        "year_offset": year_offset,
        "calendar_year": current_year + year_offset,
        "label": _label(year_offset),
        "vehicle_age": vehicle_age,
        "mileage": mileage,
        "trade_value": _round_to_50(trade_value),
        "historical": year_offset < 0,
        "projected": year_offset > 0,
    }
    if retention is not None:
        point["retention_rate"] = round(retention, 3)
        point["annual_depreciation_rate"] = round(1 - retention, 3)
    return point


def _label(year_offset: int) -> str:
    if year_offset == 0:
        return "Now"
    if year_offset < 0:
        return f"{abs(year_offset)} yr ago"
    return f"+{year_offset} yr"


def _annual_retention(age: int, make: str, model: str, body_style: str) -> float:
    loss = _base_depreciation_rate(age) + _normal_mileage_depreciation(age)
    loss += _vehicle_profile_adjustment(age, make, model, body_style)
    return max(0.72, min(0.965, 1 - loss))


def _base_depreciation_rate(age: int) -> float:
    if age <= 1:
        return 0.18
    if age == 2:
        return 0.135
    if age == 3:
        return 0.11
    if age <= 5:
        return 0.085
    if age <= 8:
        return 0.062
    if age <= 12:
        return 0.043
    return 0.03


def _normal_mileage_depreciation(age: int) -> float:
    if age <= 3:
        return 0.018
    if age <= 8:
        return 0.014
    return 0.01


def _vehicle_profile_adjustment(age: int, make: str, model: str, body_style: str) -> float:
    text = f" {make} {model} {body_style} "
    adjustment = 0.0

    if _matches(text, {"TOYOTA", "HONDA", "LEXUS", "SUBARU"}):
        adjustment -= 0.012
    if _matches(text, {"TACOMA", "TUNDRA", "4RUNNER", "WRANGLER", "911", "RAV4", "CR-V"}):
        adjustment -= 0.012
    if _matches(text, {"PICKUP", "TRUCK", "SUV", "CROSSOVER", "VAN", "MINIVAN"}):
        adjustment -= 0.006
    if _matches(
        text,
        {
            "BMW",
            "MERCEDES",
            "MERCEDES-BENZ",
            "AUDI",
            "PORSCHE",
            "CADILLAC",
            "MASERATI",
            "LAND ROVER",
            "LUXURY",
        },
    ):
        adjustment += 0.016
    if _matches(text, {"EV", "TESLA", "ELECTRIC", "BOLT", "LEAF", "IONIQ", "ID.4"}):
        adjustment += 0.024 if age <= 5 else 0.008
    if _matches(text, {"SEDAN", "HATCHBACK", "COUPE", "CONVERTIBLE"}):
        adjustment += 0.004 if age <= 6 else 0.0
    if _matches(text, {"HYUNDAI", "KIA", "NISSAN", "CHRYSLER"}):
        adjustment += 0.004

    return adjustment


def _matches(text: str, terms: set[str]) -> bool:
    return any(f" {term} " in text for term in terms)


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

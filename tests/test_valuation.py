from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

import pytest

from autotrade_pro.market_data import MarketDataBundle, MarketSignal
from autotrade_pro.trends import build_trade_value_trend
from autotrade_pro.valuation import calculate_valuation, score_condition


TRADE_IN_CASE_COUNT = 5_000
CURRENT_YEAR = datetime.now(timezone.utc).year
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "real_trade_in_vehicles.json"
REAL_VEHICLE_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
REAL_VEHICLES: list[dict[str, Any]] = REAL_VEHICLE_FIXTURE["vehicles"]
EPA_SOURCE = "EPA FuelEconomy.gov vehicles.csv"

MAKE_VALUE_MULTIPLIERS = {
    "bugatti rimac": 32.0,
    "bugatti": 28.0,
    "koenigsegg": 24.0,
    "pagani": 22.0,
    "rolls-royce": 8.5,
    "ferrari": 7.2,
    "lamborghini": 6.8,
    "mclaren": 6.2,
    "bentley": 5.6,
    "maybach": 5.2,
    "aston martin": 4.8,
    "maserati": 3.4,
    "porsche": 3.0,
    "land rover": 2.1,
    "rivian": 2.0,
    "lucid": 1.9,
    "cadillac": 1.7,
    "mercedes-benz": 1.7,
    "bmw alpina": 1.7,
    "bmw": 1.55,
    "audi": 1.5,
    "lexus": 1.4,
    "lincoln": 1.35,
    "genesis": 1.32,
    "tesla": 1.3,
    "acura": 1.22,
    "volvo": 1.2,
    "infiniti": 1.18,
    "ram": 1.16,
    "gmc": 1.15,
    "jeep": 1.12,
}

CONDITION_PROFILES = (
    (
        {
            "dents": "none",
            "interior": "clean",
            "warning_lights": "none",
            "tires": "0_6",
            "brakes": "0_6",
            "oil_change": "0_3",
        },
        ["front", "rear", "interior", "dash", "tires"],
    ),
    (
        {
            "dents": "small",
            "interior": "clean",
            "warning_lights": "none",
            "tires": "7_18",
            "brakes": "0_6",
            "oil_change": "3_6",
        },
        ["front", "rear", "interior", "dash", "tires"],
    ),
    (
        {
            "dents": "small",
            "interior": "wear",
            "warning_lights": "service",
            "tires": "7_18",
            "brakes": "7_18",
            "oil_change": "3_6",
        },
        ["front", "rear", "interior", "dash", "tires"],
    ),
    (
        {
            "dents": "multiple",
            "interior": "wear",
            "warning_lights": "service",
            "tires": "19_36",
            "brakes": "7_18",
            "oil_change": "6_12",
        },
        ["front", "rear", "interior", "dash"],
    ),
    (
        {
            "dents": "major",
            "interior": "tears",
            "warning_lights": "check_engine",
            "tires": "over_36",
            "brakes": "19_36",
            "oil_change": "over_12",
        },
        ["front", "interior"],
    ),
    (
        {
            "dents": "multiple",
            "interior": "heavy_damage",
            "warning_lights": "multiple",
            "tires": "unknown",
            "brakes": "unknown",
            "oil_change": "unknown",
        },
        ["front"],
    ),
    (
        {
            "dents": "none",
            "interior": "wear",
            "warning_lights": "none",
            "tires": "19_36",
            "brakes": "19_36",
            "oil_change": "6_12",
        },
        ["front", "rear", "dash", "tires"],
    ),
    (
        {
            "dents": "major",
            "interior": "heavy_damage",
            "warning_lights": "multiple",
            "tires": "over_36",
            "brakes": "over_36",
            "oil_change": "over_12",
        },
        ["front"],
    ),
)


@dataclass(frozen=True, slots=True)
class TradeInCase:
    case_id: str
    dealer: dict[str, Any]
    vehicle: dict[str, Any]
    mileage: int
    condition_answers: dict[str, Any]
    photo_labels: list[str]
    market: MarketDataBundle


def _mileage_for_case(index: int, age: int) -> int:
    expected = age * 12_000
    variation = (index * 7_919) % 90_000 - 30_000
    mileage = max(0, expected + variation)
    if index % 97 == 0:
        mileage = index % 8_001
    if index % 131 == 0:
        mileage = expected + 185_000 + index % 40_000
    return min(360_000, mileage)


def _retail_value_for_case(reference_new_value: int, age: int, mileage: int) -> int:
    expected = age * 12_000
    depreciation = min(0.9, 0.085 * age + 0.012 * max(age - 4, 0))
    mileage_factor = max(0.68, min(1.2, 1 + (expected - mileage) / 250_000))
    return _round_to_50(
        max(1_800, reference_new_value * (1 - depreciation) * mileage_factor)
    )


def _reference_new_value_for_vehicle(vehicle: dict[str, Any]) -> int:
    vehicle_class = str(vehicle.get("body_style") or "").lower()
    base_value = 32_000
    if "subcompact" in vehicle_class or "minicompact" in vehicle_class:
        base_value = 25_000
    elif "compact" in vehicle_class:
        base_value = 29_000
    elif "midsize" in vehicle_class:
        base_value = 34_000
    elif "large" in vehicle_class:
        base_value = 42_000
    elif "two seaters" in vehicle_class:
        base_value = 58_000
    elif "small pickup" in vehicle_class:
        base_value = 36_000
    elif "standard pickup" in vehicle_class:
        base_value = 48_000
    elif "small sport utility" in vehicle_class:
        base_value = 39_000
    elif "standard sport utility" in vehicle_class:
        base_value = 58_000
    elif "sport utility" in vehicle_class:
        base_value = 44_000
    elif "vans" in vehicle_class:
        base_value = 39_000
    elif "station wagons" in vehicle_class:
        base_value = 35_000
    elif "special purpose" in vehicle_class:
        base_value = 36_000

    fuel_type = str(vehicle.get("fuel_type") or "").lower()
    if "electricity" in fuel_type:
        base_value += 7_500
    if "diesel" in fuel_type:
        base_value += 2_500

    cylinders = vehicle.get("cylinders")
    if isinstance(cylinders, int):
        base_value += max(0, cylinders - 4) * 1_200

    displacement = vehicle.get("displacement_liters")
    if isinstance(displacement, (int, float)) and displacement >= 5:
        base_value += 3_500

    make = str(vehicle.get("make") or "").lower()
    multiplier = next(
        (value for key, value in MAKE_VALUE_MULTIPLIERS.items() if key in make),
        1.0,
    )
    return _round_to_50(base_value * multiplier)


def _round_to_50(value: float) -> int:
    return int(round(value / 50) * 50)


def _vin_for_case(index: int, year: int) -> str:
    return f"ATP{year % 100:02d}{index:012d}"


def _build_trade_in_cases(count: int = TRADE_IN_CASE_COUNT) -> list[TradeInCase]:
    if len(REAL_VEHICLES) < count:
        raise RuntimeError(f"Real vehicle fixture only contains {len(REAL_VEHICLES):,} rows")

    cases: list[TradeInCase] = []
    for index, source_vehicle in enumerate(REAL_VEHICLES[:count]):
        year = int(source_vehicle["year"])
        age = max(0, CURRENT_YEAR - year)
        mileage = _mileage_for_case(index, age)
        reference_new_value = _reference_new_value_for_vehicle(source_vehicle)
        retail_value = _retail_value_for_case(reference_new_value, age, mileage)
        wholesale_value = _round_to_50(
            min(retail_value * 0.9, max(900, retail_value * (0.64 + (index % 9) * 0.025)))
        )
        comparable_value = _round_to_50(retail_value * (0.92 + (index % 11) * 0.018))
        confidence = round(0.45 + (index % 53) / 100, 2)
        answers, photo_labels = CONDITION_PROFILES[index % len(CONDITION_PROFILES)]
        case_id = _case_id(index, source_vehicle)
        market = MarketDataBundle(
            region="national_test_matrix",
            auction_value=wholesale_value,
            retail_value=retail_value,
            comparable_value=comparable_value,
            confidence=confidence,
            signals=[
                MarketSignal(
                    source="epa_real_vehicle_test_matrix",
                    retail_value=retail_value,
                    wholesale_value=wholesale_value,
                    sample_size=8 + index % 120,
                    days_supply=10 + index % 95,
                    confidence=confidence,
                    raw={
                        "case_id": case_id,
                        "source": source_vehicle["source"],
                        "source_id": source_vehicle["source_id"],
                        "reference_new_value": reference_new_value,
                        "value_basis": "deterministic_test_scenario",
                    },
                )
            ],
            notes=[],
        )
        cases.append(
            TradeInCase(
                case_id=case_id,
                dealer={
                    "max_retail_percent": 0.92 + (index % 5) * 0.01,
                    "valuation_hold_days": 7 + index % 14,
                },
                vehicle={
                    "vin": _vin_for_case(index, year),
                    "year": year,
                    "make": source_vehicle["make"],
                    "model": source_vehicle["model"],
                    "trim": source_vehicle["trim"],
                    "body_style": source_vehicle["body_style"],
                    "drive": source_vehicle["drive"],
                    "fuel_type": source_vehicle["fuel_type"],
                    "source": source_vehicle["source"],
                    "source_id": source_vehicle["source_id"],
                },
                mileage=mileage,
                condition_answers=dict(answers),
                photo_labels=list(photo_labels),
                market=market,
            )
        )
    return cases


def _case_id(index: int, vehicle: dict[str, Any]) -> str:
    identity = (
        f"{index:04d}-{vehicle['year']}-{vehicle['make']}-"
        f"{vehicle['model']}-{vehicle['source_id']}"
    )
    return re.sub(r"[^a-z0-9]+", "-", identity.lower()).strip("-")


TRADE_IN_CASES = _build_trade_in_cases()


def test_offer_never_exceeds_retail_cap():
    market = MarketDataBundle(
        region="south_florida",
        auction_value=25000,
        retail_value=20000,
        comparable_value=26000,
        confidence=0.92,
        signals=[
            MarketSignal(
                source="test",
                retail_value=20000,
                wholesale_value=25000,
                sample_size=20,
                days_supply=30,
                confidence=0.9,
                raw={},
            )
        ],
        notes=[],
    )
    result = calculate_valuation(
        dealer={"max_retail_percent": 0.95, "valuation_hold_days": 10},
        vehicle={
            "vin": "1HGCM82633A004352",
            "year": 2024,
            "make": "HONDA",
            "model": "ACCORD",
        },
        mileage=1000,
        condition_answers={
            "dents": "none",
            "interior": "clean",
            "warning_lights": "none",
            "tires": "0_6",
            "brakes": "0_6",
            "oil_change": "0_3",
        },
        photo_labels=["front", "rear", "interior", "dash", "tires"],
        market=market,
    )

    assert result.trade_offer <= 19000
    assert result.cap_value == 19000


def test_condition_score_penalizes_missing_photos_and_damage():
    score, grade, adjustments = score_condition(
        {
            "dents": "major",
            "interior": "tears",
            "warning_lights": "check_engine",
            "tires": "over_36",
            "brakes": "unknown",
            "oil_change": "over_12",
        },
        ["front"],
    )

    assert score < 62
    assert grade == "Needs Review"
    assert "photo_completeness" in adjustments["penalties"]


def test_trade_in_case_matrix_has_wide_vehicle_spread():
    years = [case.vehicle["year"] for case in TRADE_IN_CASES]
    mileages = [case.mileage for case in TRADE_IN_CASES]
    makes = {case.vehicle["make"] for case in TRADE_IN_CASES}
    models = {case.vehicle["model"] for case in TRADE_IN_CASES}
    body_styles = {case.vehicle["body_style"] for case in TRADE_IN_CASES}
    source_ids = {case.vehicle["source_id"] for case in TRADE_IN_CASES}
    grades = {
        score_condition(case.condition_answers, case.photo_labels)[1]
        for case in TRADE_IN_CASES
    }
    retail_values = [case.market.retail_value for case in TRADE_IN_CASES]

    assert len(TRADE_IN_CASES) == 5_000
    assert REAL_VEHICLE_FIXTURE["metadata"]["source"] == EPA_SOURCE
    assert len(source_ids) == 5_000
    assert {case.vehicle["source"] for case in TRADE_IN_CASES} == {EPA_SOURCE}
    assert min(years) <= 1985
    assert max(years) >= 2026
    assert min(mileages) == 0
    assert max(mileages) >= 300_000
    assert len(makes) >= 80
    assert len(models) >= 2_000
    assert {
        "Compact Cars",
        "Midsize Cars",
        "Standard Pickup Trucks",
        "Small Sport Utility Vehicle 4WD",
        "Two Seaters",
        "Vans",
    }.issubset(body_styles)
    assert grades == {"Excellent", "Good", "Fair", "Needs Review"}
    assert min(retail_values) <= 2_500
    assert max(retail_values) >= 125_000


@pytest.mark.parametrize("case", TRADE_IN_CASES, ids=lambda case: case.case_id)
def test_trade_in_case_matrix_values_each_trade(case: TradeInCase):
    result = calculate_valuation(
        dealer=case.dealer,
        vehicle=case.vehicle,
        mileage=case.mileage,
        condition_answers=case.condition_answers,
        photo_labels=case.photo_labels,
        market=case.market,
    )

    assert result.public_id.startswith("ATP-")
    assert result.cap_value % 50 == 0
    assert result.trade_offer % 50 == 0
    assert result.valuation_low % 50 == 0
    assert result.valuation_high % 50 == 0
    assert 500 <= result.trade_offer <= result.cap_value
    assert result.valuation_low <= result.trade_offer <= result.valuation_high
    assert result.valuation_high <= result.cap_value
    assert 35 <= result.condition_score <= 100
    assert result.condition_grade in {"Excellent", "Good", "Fair", "Needs Review"}
    assert 0 <= result.data_quality_score <= 1
    assert result.retail_market_value == case.market.retail_value
    assert result.auction_wholesale_value == case.market.auction_value
    assert result.comparable_value == case.market.comparable_value
    assert result.adjustments["mileage"]["reported_mileage"] == case.mileage
    assert result.source_breakdown["signals"][0]["raw"]["case_id"] == case.case_id
    assert result.source_breakdown["signals"][0]["raw"]["source"] == EPA_SOURCE


def test_trend_curve_depreciates_newer_and_luxury_vehicles_faster():
    current_year = datetime.now(timezone.utc).year
    new_luxury = build_trade_value_trend(
        {
            "trade_offer": 30000,
            "mileage": 12000,
            "year": current_year - 1,
            "make": "BMW",
            "model": "3 Series",
            "body_style": "Luxury Sedan",
        }
    )
    older_reliable_suv = build_trade_value_trend(
        {
            "trade_offer": 30000,
            "mileage": 96000,
            "year": current_year - 8,
            "make": "HONDA",
            "model": "CR-V",
            "body_style": "SUV",
        }
    )

    new_now = next(point for point in new_luxury["points"] if point["year_offset"] == 0)
    new_year_one = next(point for point in new_luxury["points"] if point["year_offset"] == 1)
    old_now = next(point for point in older_reliable_suv["points"] if point["year_offset"] == 0)
    old_year_one = next(point for point in older_reliable_suv["points"] if point["year_offset"] == 1)

    assert new_year_one["annual_depreciation_rate"] > old_year_one["annual_depreciation_rate"]
    assert new_now["trade_value"] - new_year_one["trade_value"] > old_now["trade_value"] - old_year_one["trade_value"]
    assert new_luxury["points"][-1]["trade_value"] < older_reliable_suv["points"][-1]["trade_value"]
    assert older_reliable_suv["points"][0]["year_offset"] == -5

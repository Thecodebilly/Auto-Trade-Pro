from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from autotrade_pro.market_data import MarketDataBundle, MarketSignal
from autotrade_pro.valuation import calculate_valuation, score_condition


TRADE_IN_CASE_COUNT = 5_000
CURRENT_YEAR = datetime.now(timezone.utc).year

VEHICLE_CATALOG = (
    ("TOYOTA", "COROLLA", "LE", "Sedan", 23500),
    ("HONDA", "CIVIC", "EX", "Sedan", 26500),
    ("FORD", "F-150", "Lariat", "Pickup", 58500),
    ("CHEVROLET", "SILVERADO", "LT", "Pickup", 54000),
    ("TESLA", "MODEL 3", "Long Range", "EV Sedan", 47000),
    ("BMW", "3 SERIES", "330i", "Sedan", 52000),
    ("MERCEDES-BENZ", "S-CLASS", "S 580", "Luxury Sedan", 124000),
    ("LEXUS", "RX", "350", "SUV", 61000),
    ("SUBARU", "OUTBACK", "Limited", "Wagon", 39000),
    ("JEEP", "WRANGLER", "Rubicon", "SUV", 52000),
    ("RAM", "1500", "Big Horn", "Pickup", 57000),
    ("PORSCHE", "911", "Carrera", "Coupe", 142000),
    ("NISSAN", "LEAF", "SV", "EV Hatchback", 32500),
    ("HYUNDAI", "ELANTRA", "SEL", "Sedan", 25000),
    ("KIA", "TELLURIDE", "SX", "SUV", 50000),
    ("DODGE", "GRAND CARAVAN", "SXT", "Minivan", 34000),
    ("MAZDA", "MX-5 MIATA", "Grand Touring", "Convertible", 36000),
    ("GMC", "YUKON", "Denali", "SUV", 78000),
    ("TOYOTA", "RAV4", "XLE", "SUV", 36000),
    ("HONDA", "CR-V", "EX-L", "SUV", 38500),
    ("FORD", "MUSTANG", "GT", "Coupe", 48000),
    ("CHEVROLET", "BOLT", "Premier", "EV Hatchback", 31500),
    ("VOLVO", "XC90", "Inscription", "SUV", 68000),
    ("TOYOTA", "PRIUS", "XLE", "Hybrid Hatchback", 33000),
    ("ACURA", "MDX", "Technology", "SUV", 59000),
    ("CADILLAC", "ESCALADE", "Premium Luxury", "SUV", 102000),
    ("MINI", "COOPER", "S", "Hatchback", 34000),
    ("CHRYSLER", "PACIFICA", "Touring L", "Minivan", 48500),
)

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


def _retail_value_for_case(base_msrp: int, age: int, mileage: int) -> int:
    expected = age * 12_000
    depreciation = min(0.9, 0.085 * age + 0.012 * max(age - 4, 0))
    mileage_factor = max(0.68, min(1.2, 1 + (expected - mileage) / 250_000))
    return _round_to_50(max(1_800, base_msrp * (1 - depreciation) * mileage_factor))


def _round_to_50(value: float) -> int:
    return int(round(value / 50) * 50)


def _vin_for_case(index: int, year: int) -> str:
    return f"ATP{year % 100:02d}{index:012d}"


def _build_trade_in_cases(count: int = TRADE_IN_CASE_COUNT) -> list[TradeInCase]:
    cases: list[TradeInCase] = []
    for index in range(count):
        make, model, trim, body_style, base_msrp = VEHICLE_CATALOG[index % len(VEHICLE_CATALOG)]
        age = (index * 37 + index // 11) % 46
        year = CURRENT_YEAR - age
        mileage = _mileage_for_case(index, age)
        retail_value = _retail_value_for_case(base_msrp, age, mileage)
        wholesale_value = _round_to_50(
            min(retail_value * 0.9, max(900, retail_value * (0.64 + (index % 9) * 0.025)))
        )
        comparable_value = _round_to_50(retail_value * (0.92 + (index % 11) * 0.018))
        confidence = round(0.45 + (index % 53) / 100, 2)
        answers, photo_labels = CONDITION_PROFILES[index % len(CONDITION_PROFILES)]
        case_id = f"{index:04d}-{year}-{make}-{model}".lower().replace(" ", "-")
        market = MarketDataBundle(
            region="national_test_matrix",
            auction_value=wholesale_value,
            retail_value=retail_value,
            comparable_value=comparable_value,
            confidence=confidence,
            signals=[
                MarketSignal(
                    source="synthetic_trade_in_matrix",
                    retail_value=retail_value,
                    wholesale_value=wholesale_value,
                    sample_size=8 + index % 120,
                    days_supply=10 + index % 95,
                    confidence=confidence,
                    raw={"case_id": case_id, "base_msrp": base_msrp},
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
                    "make": make,
                    "model": model,
                    "trim": trim,
                    "body_style": body_style,
                },
                mileage=mileage,
                condition_answers=dict(answers),
                photo_labels=list(photo_labels),
                market=market,
            )
        )
    return cases


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
        vehicle={"vin": "1HGCM82633A004352", "year": 2024, "make": "HONDA", "model": "ACCORD"},
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
    body_styles = {case.vehicle["body_style"] for case in TRADE_IN_CASES}
    grades = {
        score_condition(case.condition_answers, case.photo_labels)[1]
        for case in TRADE_IN_CASES
    }
    retail_values = [case.market.retail_value for case in TRADE_IN_CASES]

    assert len(TRADE_IN_CASES) == 5_000
    assert min(years) <= CURRENT_YEAR - 40
    assert max(years) == CURRENT_YEAR
    assert min(mileages) == 0
    assert max(mileages) >= 300_000
    assert len(makes) >= 18
    assert {
        "Sedan",
        "SUV",
        "Pickup",
        "Coupe",
        "Convertible",
        "Minivan",
        "EV Hatchback",
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

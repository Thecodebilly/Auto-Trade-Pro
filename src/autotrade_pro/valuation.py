"""Trade-in valuation engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import secrets
from typing import Any

from .market_data import MarketDataBundle


@dataclass(slots=True)
class ValuationResult:
    public_id: str
    condition_score: float
    condition_grade: str
    retail_market_value: int
    auction_wholesale_value: int
    comparable_value: int
    cap_value: int
    trade_offer: int
    valuation_low: int
    valuation_high: int
    data_quality_score: float
    source_breakdown: dict[str, Any]
    adjustments: dict[str, Any]
    offer_expires_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "public_id": self.public_id,
            "condition_score": self.condition_score,
            "condition_grade": self.condition_grade,
            "retail_market_value": self.retail_market_value,
            "auction_wholesale_value": self.auction_wholesale_value,
            "comparable_value": self.comparable_value,
            "cap_value": self.cap_value,
            "trade_offer": self.trade_offer,
            "valuation_low": self.valuation_low,
            "valuation_high": self.valuation_high,
            "data_quality_score": self.data_quality_score,
            "source_breakdown": self.source_breakdown,
            "adjustments": self.adjustments,
            "offer_expires_at": self.offer_expires_at,
        }


def calculate_valuation(
    *,
    dealer: dict[str, Any],
    vehicle: dict[str, Any],
    mileage: int,
    condition_answers: dict[str, Any],
    photo_labels: list[str],
    market: MarketDataBundle,
) -> ValuationResult:
    condition_score, condition_grade, condition_adjustments = score_condition(
        condition_answers, photo_labels
    )

    year = _int_or_none(vehicle.get("year"))
    current_year = datetime.now(timezone.utc).year
    age = max(0, current_year - year) if year else 5
    expected_mileage = max(12000, age * 12000)
    mileage_delta = mileage - expected_mileage
    mileage_adjustment = _round_to_50(-mileage_delta * _mileage_rate(age))

    auction_value = int(market.auction_value)
    retail_value = int(market.retail_value)
    comparable_value = int(market.comparable_value)
    market_baseline = auction_value * 0.68 + comparable_value * 0.22 + retail_value * 0.10

    condition_index = condition_score / 100
    condition_adjustment = (condition_index - 0.82) * retail_value * 0.45
    reconditioning_reserve = _reconditioning_reserve(condition_score, condition_answers)
    raw_offer = market_baseline + condition_adjustment + mileage_adjustment - reconditioning_reserve

    max_retail_percent = float(dealer.get("max_retail_percent") or 0.95)
    cap_value = _round_to_50(retail_value * max_retail_percent)
    trade_offer = max(500, min(_round_to_50(raw_offer), cap_value))
    valuation_low = max(500, _round_to_50(trade_offer * 0.97))
    valuation_high = min(cap_value, _round_to_50(trade_offer * 1.03))

    data_quality = _data_quality_score(market, vehicle, photo_labels)
    source_breakdown = {
        "weights": {
            "condition_assessment": 0.45,
            "auction_history": 0.35,
            "market_comparables": 0.20,
        },
        "market_confidence": market.confidence,
        "signals": [signal.to_dict() for signal in market.signals],
        "notes": market.notes,
        "retail_cap_rule": f"Offer capped at {max_retail_percent:.0%} of retail market value.",
    }
    adjustments = {
        "condition": condition_adjustments,
        "condition_adjustment": _round_to_50(condition_adjustment),
        "mileage": {
            "reported_mileage": mileage,
            "expected_mileage": expected_mileage,
            "delta": mileage_delta,
            "adjustment": mileage_adjustment,
        },
        "reconditioning_reserve": reconditioning_reserve,
        "market_baseline": _round_to_50(market_baseline),
        "uncapped_offer": _round_to_50(raw_offer),
    }
    hold_days = int(dealer.get("valuation_hold_days") or 10)
    expires = datetime.now(timezone.utc) + timedelta(days=hold_days)

    return ValuationResult(
        public_id=f"ATP-{secrets.token_urlsafe(8).replace('_', '').replace('-', '').upper()[:10]}",
        condition_score=round(condition_score, 1),
        condition_grade=condition_grade,
        retail_market_value=retail_value,
        auction_wholesale_value=auction_value,
        comparable_value=comparable_value,
        cap_value=cap_value,
        trade_offer=trade_offer,
        valuation_low=valuation_low,
        valuation_high=valuation_high,
        data_quality_score=round(data_quality, 2),
        source_breakdown=source_breakdown,
        adjustments=adjustments,
        offer_expires_at=expires.isoformat(timespec="seconds"),
    )


def build_valuation_record(
    *,
    dealer: dict[str, Any],
    vehicle: dict[str, Any],
    mileage: int,
    condition_answers: dict[str, Any],
    photo_summary: list[dict[str, Any]],
    market: MarketDataBundle,
    result: ValuationResult,
) -> dict[str, Any]:
    return {
        "public_id": result.public_id,
        "dealer_id": dealer["id"],
        "vin": vehicle.get("vin", ""),
        "year": _int_or_none(vehicle.get("year")),
        "make": vehicle.get("make", ""),
        "model": vehicle.get("model", ""),
        "trim": vehicle.get("trim", ""),
        "body_style": vehicle.get("body_style", ""),
        "mileage": mileage,
        "condition_answers_json": _json(condition_answers),
        "photo_summary_json": _json(photo_summary),
        "vehicle_payload_json": _json(vehicle),
        "source_data_json": _json(market.to_dict()),
        "adjustments_json": _json(result.adjustments),
        "source_breakdown_json": _json(result.source_breakdown),
        "condition_score": result.condition_score,
        "condition_grade": result.condition_grade,
        "retail_market_value": result.retail_market_value,
        "auction_wholesale_value": result.auction_wholesale_value,
        "comparable_value": result.comparable_value,
        "cap_value": result.cap_value,
        "trade_offer": result.trade_offer,
        "valuation_low": result.valuation_low,
        "valuation_high": result.valuation_high,
        "data_quality_score": result.data_quality_score,
        "offer_expires_at": result.offer_expires_at,
    }


def score_condition(
    answers: dict[str, Any], photo_labels: list[str] | None = None
) -> tuple[float, str, dict[str, Any]]:
    photo_labels = photo_labels or []
    score = 100.0
    penalties: dict[str, float] = {}

    penalty_maps = {
        "dents": {"none": 0, "small": 6, "multiple": 13, "major": 22},
        "interior": {"clean": 0, "wear": 5, "tears": 13, "heavy_damage": 22},
        "warning_lights": {"none": 0, "service": 7, "check_engine": 16, "multiple": 25},
        "tires": {"0_6": 0, "7_18": 4, "19_36": 9, "over_36": 14, "unknown": 7},
        "brakes": {"0_6": 0, "7_18": 4, "19_36": 8, "over_36": 13, "unknown": 6},
        "oil_change": {"0_3": 0, "3_6": 3, "6_12": 7, "over_12": 11, "unknown": 5},
    }

    for key, mapping in penalty_maps.items():
        value = str(answers.get(key, "unknown")).strip()
        penalty = float(mapping.get(value, mapping.get("unknown", 0)))
        if penalty:
            penalties[key] = penalty
            score -= penalty

    required_photos = {"front", "rear", "interior", "dash", "tires"}
    provided = {str(label).lower() for label in photo_labels}
    missing = sorted(required_photos - provided)
    if missing:
        photo_penalty = min(10, len(missing) * 2.5)
        penalties["photo_completeness"] = photo_penalty
        score -= photo_penalty

    score = max(35.0, min(100.0, score))
    if score >= 90:
        grade = "Excellent"
    elif score >= 78:
        grade = "Good"
    elif score >= 62:
        grade = "Fair"
    else:
        grade = "Needs Review"
    return score, grade, {"penalties": penalties, "missing_photos": missing}


def _reconditioning_reserve(score: float, answers: dict[str, Any]) -> int:
    base = max(250, int((100 - score) * 48))
    if answers.get("warning_lights") in {"check_engine", "multiple"}:
        base += 750
    if answers.get("dents") == "major":
        base += 600
    if answers.get("interior") in {"tears", "heavy_damage"}:
        base += 450
    return _round_to_50(base)


def _data_quality_score(
    market: MarketDataBundle, vehicle: dict[str, Any], photo_labels: list[str]
) -> float:
    vehicle_completeness = sum(
        1 for key in ["vin", "year", "make", "model"] if vehicle.get(key)
    ) / 4
    photo_completeness = min(1.0, len(set(photo_labels)) / 5)
    return min(1.0, market.confidence * 0.5 + vehicle_completeness * 0.25 + photo_completeness * 0.25)


def _mileage_rate(age: int) -> float:
    if age <= 2:
        return 0.085
    if age <= 5:
        return 0.065
    return 0.045


def _round_to_50(value: float) -> int:
    if math.isnan(value) or math.isinf(value):
        return 0
    return int(round(value / 50) * 50)


def _int_or_none(value: object) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)

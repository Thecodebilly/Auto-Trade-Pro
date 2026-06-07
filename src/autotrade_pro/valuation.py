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

    # Age-aware market weighting: near-new cars trade closer to retail;
    # older cars are more auction/wholesale driven.
    auction_w, comparable_w, retail_w = _market_weights(age)
    market_baseline = (
        auction_value * auction_w
        + comparable_value * comparable_w
        + retail_value * retail_w
    )

    # Condition sensitivity scales with age: a dent on a 1-year-old car
    # matters far more than on a 10-year-old car where wear is expected.
    condition_index = condition_score / 100
    cond_sensitivity = _condition_sensitivity(age)
    condition_adjustment = (condition_index - 0.82) * retail_value * cond_sensitivity

    reconditioning_reserve = _reconditioning_reserve(condition_score, condition_answers)
    raw_offer = (
        market_baseline
        + condition_adjustment
        + mileage_adjustment
        - reconditioning_reserve
    )

    # Make/model retention factor: some vehicles (Tacoma, Wrangler, F-150)
    # hold value far better than others (luxury sedans, Maserati, Fiat).
    make = str(vehicle.get("make", "") or "").upper().strip()
    model = str(vehicle.get("model", "") or "").upper().strip()
    retention_factor, retention_label = _retention_factor(make, model)
    retained_offer = raw_offer * retention_factor

    max_retail_percent = float(dealer.get("max_retail_percent") or 0.95)
    cap_value = _round_to_50(retail_value * max_retail_percent)
    trade_offer = max(500, min(_round_to_50(retained_offer), cap_value))
    valuation_low = max(500, _round_to_50(trade_offer * 0.97))
    valuation_high = min(cap_value, _round_to_50(trade_offer * 1.03))

    data_quality = _data_quality_score(market, vehicle, photo_labels)
    source_breakdown = {
        "weights": {
            "auction_wholesale": round(auction_w, 2),
            "market_comparables": round(comparable_w, 2),
            "retail_reference": round(retail_w, 2),
        },
        "age_years": age,
        "retention_profile": retention_label,
        "retention_factor": round(retention_factor, 3),
        "condition_sensitivity": round(cond_sensitivity, 2),
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
            "rate_per_mile": _mileage_rate(age),
        },
        "reconditioning_reserve": reconditioning_reserve,
        "market_baseline": _round_to_50(market_baseline),
        "retention_adjustment": _round_to_50(retained_offer - raw_offer),
        "uncapped_offer": _round_to_50(retained_offer),
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
    vehicle_completeness = (
        sum(1 for key in ["vin", "year", "make", "model"] if vehicle.get(key)) / 4
    )
    photo_completeness = min(1.0, len(set(photo_labels)) / 5)
    return min(
        1.0,
        market.confidence * 0.5
        + vehicle_completeness * 0.25
        + photo_completeness * 0.25,
    )


def _market_weights(age: int) -> tuple[float, float, float]:
    """Return (auction_weight, comparable_weight, retail_weight) by vehicle age.

    Near-new cars (0-1 yrs) haven't fully left the retail market yet, so the
    offer should reflect retail pricing more heavily.  As cars age, wholesale
    auction data becomes the dominant signal.
    """
    if age <= 1:
        return (0.42, 0.33, 0.25)  # 0-1 yr: still close to retail/MSRP
    if age <= 2:
        return (0.52, 0.30, 0.18)  # 1-2 yr: first steep-drop zone
    if age <= 4:
        return (0.63, 0.26, 0.11)  # 3-4 yr: mid-depreciation
    if age <= 7:
        return (0.68, 0.22, 0.10)  # 5-7 yr: stable used-car market
    if age <= 12:
        return (0.73, 0.20, 0.07)  # 8-12 yr: auction-driven
    return (0.78, 0.17, 0.05)  # 13+ yr: collector/wholesale territory


def _condition_sensitivity(age: int) -> float:
    """Multiplier for how much the condition score moves the offer.

    A scratch on a 1-year-old car is a much bigger deal (proportionally) than
    on a 10-year-old car where some wear is already baked into the market price.
    """
    if age <= 1:
        return 0.58  # Near-new: every defect is heavily penalised
    if age <= 3:
        return 0.52
    if age <= 6:
        return 0.45  # Baseline (original value)
    if age <= 10:
        return 0.38
    return 0.30  # Old cars: wear is expected; condition matters less


def _mileage_rate(age: int) -> float:
    """Dollar impact per mile of deviation from the expected annual mileage baseline.

    New cars command a higher per-mile premium/discount because every mile
    matters more when the market compares against factory-fresh units.
    Older cars have a flatter $/mile curve.
    """
    if age <= 1:
        return 0.14  # ~$140 per 1 000 miles above/below expected
    if age <= 2:
        return 0.10
    if age <= 4:
        return 0.075
    if age <= 7:
        return 0.058
    if age <= 12:
        return 0.042
    return 0.028  # Very old cars: mileage is largely irrelevant


# ---------------------------------------------------------------------------
# Make / model retention profiles
# ---------------------------------------------------------------------------

# Factor > 1.0 → vehicle holds value well → offer bonus
# Factor < 1.0 → vehicle depreciates fast  → offer reduction
# Structure: MAKE → {MODEL_FRAGMENT: factor, "": make_default}
# Model matching uses a startswith check on the normalised model string so
# "F-150" matches "F-150 SUPERCREW", "LARIAT" etc.

_RETENTION_PROFILES: dict[str, dict[str, float]] = {
    # ── Strong value-holders ────────────────────────────────────────────────
    "TOYOTA": {
        "": 1.03,
        "TACOMA": 1.09,
        "4RUNNER": 1.09,
        "LAND CRUISER": 1.07,
        "TUNDRA": 1.06,
        "SEQUOIA": 1.04,
        "RAV4": 1.04,
        "HIGHLANDER": 1.03,
        "CAMRY": 1.03,
        "SIENNA": 1.02,
        "COROLLA": 1.02,
        "PRIUS": 1.02,
    },
    "HONDA": {
        "": 1.02,
        "CR-V": 1.04,
        "RIDGELINE": 1.04,
        "PILOT": 1.03,
        "PASSPORT": 1.02,
        "ACCORD": 1.02,
        "ODYSSEY": 1.02,
        "CIVIC": 1.01,
    },
    "SUBARU": {
        "": 1.02,
        "WRX": 1.05,
        "BRZ": 1.04,
        "OUTBACK": 1.04,
        "FORESTER": 1.04,
        "CROSSTREK": 1.03,
        "ASCENT": 1.02,
    },
    "JEEP": {
        "": 1.01,
        "WRANGLER": 1.09,
        "GLADIATOR": 1.06,
        "GRAND CHEROKEE": 1.02,
        "GRAND WAGONEER": 1.03,
    },
    "FORD": {
        "": 1.00,
        "F-250": 1.05,
        "F-350": 1.05,
        "F-150": 1.05,
        "BRONCO": 1.06,
        "MAVERICK": 1.03,
        "RANGER": 1.02,
    },
    "RAM": {
        "": 1.01,
        "3500": 1.06,
        "2500": 1.05,
        "1500": 1.04,
    },
    "GMC": {
        "": 1.01,
        "SIERRA 2500": 1.05,
        "SIERRA 3500": 1.06,
        "SIERRA": 1.04,
        "YUKON": 1.03,
        "CANYON": 1.02,
        "ACADIA": 1.01,
    },
    "CHEVROLET": {
        "": 1.00,
        "SILVERADO 2500": 1.05,
        "SILVERADO 3500": 1.06,
        "SILVERADO": 1.04,
        "TAHOE": 1.03,
        "SUBURBAN": 1.03,
        "COLORADO": 1.02,
        "BLAZER": 1.01,
        "TRAX": 0.98,
    },
    "PORSCHE": {
        "": 1.04,
        "911": 1.10,
        "718": 1.06,
        "CAYENNE": 1.05,
        "MACAN": 1.03,
        "PANAMERA": 1.02,
        "TAYCAN": 1.01,
    },
    "TESLA": {
        "": 1.01,
        "MODEL Y": 1.03,
        "MODEL 3": 1.02,
        "MODEL X": 1.01,
        "MODEL S": 1.00,
    },
    "LEXUS": {
        "": 1.01,
        "LX": 1.06,
        "GX": 1.05,
        "RX": 1.04,
        "NX": 1.02,
        "IS": 1.02,
        "ES": 1.02,
        "LC": 1.04,
    },
    "ACURA": {
        "": 0.97,
        "NSX": 1.05,
        "MDX": 0.98,
        "INTEGRA": 0.99,
        "TLX": 0.97,
    },
    "MAZDA": {
        "": 1.00,
        "MX-5 MIATA": 1.04,
        "MX-5": 1.04,
        "CX-5": 1.02,
        "CX-9": 1.01,
        "CX-50": 1.02,
    },
    # ── Moderate / average depreciation ─────────────────────────────────────
    "NISSAN": {
        "": 0.99,
        "GT-R": 1.04,
        "FRONTIER": 1.02,
        "ARMADA": 1.00,
        "PATHFINDER": 0.99,
        "ALTIMA": 0.98,
        "SENTRA": 0.97,
        "KICKS": 0.96,
        "VERSA": 0.95,
    },
    "KIA": {
        "": 0.98,
        "TELLURIDE": 1.04,
        "STINGER": 1.01,
        "SORENTO": 1.00,
        "SPORTAGE": 0.99,
        "K5": 0.98,
        "FORTE": 0.96,
    },
    "HYUNDAI": {
        "": 0.98,
        "PALISADE": 1.02,
        "IONIQ 6": 1.01,
        "IONIQ 5": 1.01,
        "SANTA FE": 1.00,
        "TUCSON": 0.99,
        "SONATA": 0.97,
        "ELANTRA": 0.97,
    },
    "VOLKSWAGEN": {
        "": 0.97,
        "GOLF GTI": 1.00,
        "GOLF R": 1.02,
        "ATLAS": 0.97,
        "TIGUAN": 0.97,
        "ID.4": 0.94,
        "PASSAT": 0.93,
    },
    "DODGE": {
        "": 0.95,
        "CHALLENGER": 1.01,  # Hellcat/SRT holds value
        "CHARGER": 0.98,
        "DURANGO": 0.97,
        "JOURNEY": 0.90,
    },
    # ── Fast depreciators ────────────────────────────────────────────────────
    "BMW": {
        "": 0.93,
        "M2": 1.02,
        "M3": 1.01,
        "M4": 1.01,
        "M5": 1.00,
        "X5": 0.95,
        "X7": 0.94,
        "3 SERIES": 0.93,
        "5 SERIES": 0.92,
        "7 SERIES": 0.89,
        "I4": 0.91,
        "IX": 0.90,
    },
    "MERCEDES-BENZ": {
        "": 0.92,
        "G-CLASS": 1.07,  # G-Wagon holds value exceptionally well
        "G CLASS": 1.07,
        "AMG GT": 1.02,
        "C-CLASS": 0.93,
        "GLE": 0.93,
        "GLC": 0.93,
        "E-CLASS": 0.91,
        "GLS": 0.91,
        "S-CLASS": 0.88,
        "EQS": 0.87,
    },
    "MERCEDES": {
        "": 0.92,
        "G-CLASS": 1.07,
        "G CLASS": 1.07,
    },
    "AUDI": {
        "": 0.93,
        "Q5": 0.95,
        "Q7": 0.92,
        "RS": 1.00,  # RS models (starts with RS) hold better
        "A4": 0.93,
        "A6": 0.91,
        "A7": 0.91,
        "A8": 0.88,
        "E-TRON": 0.89,
        "Q4": 0.91,
    },
    "JAGUAR": {
        "": 0.89,
        "F-TYPE": 0.93,
        "F-PACE": 0.91,
        "E-PACE": 0.88,
        "I-PACE": 0.87,
    },
    "LAND ROVER": {
        "": 0.90,
        "DEFENDER": 1.04,  # Defender is the exception
        "RANGE ROVER": 0.92,
        "DISCOVERY": 0.90,
        "SPORT": 0.90,
    },
    "MASERATI": {
        "": 0.84,
        "MC20": 0.97,  # Supercar holds better
        "GHIBLI": 0.82,
        "QUATTROPORTE": 0.81,
    },
    "ALFA ROMEO": {
        "": 0.89,
        "GIULIA": 0.90,
        "STELVIO": 0.90,
    },
    "CADILLAC": {
        "": 0.93,
        "ESCALADE": 1.02,
        "CT5-V BLACKWING": 1.00,
        "CT5-V": 0.99,
        "CT5": 0.94,
        "XT5": 0.94,
        "LYRIQ": 0.91,
    },
    "LINCOLN": {
        "": 0.92,
        "NAVIGATOR": 1.01,
        "AVIATOR": 0.94,
        "CORSAIR": 0.92,
    },
    "BUICK": {
        "": 0.94,
        "ENVISION": 0.94,
        "ENCLAVE": 0.95,
    },
    "CHRYSLER": {
        "": 0.91,
        "300": 0.91,
        "PACIFICA": 0.93,
    },
    "MITSUBISHI": {
        "": 0.92,
        "OUTLANDER": 0.94,
        "ECLIPSE CROSS": 0.91,
        "MIRAGE": 0.88,
    },
    "FIAT": {
        "": 0.88,
        "500X": 0.89,
        "500": 0.87,
    },
    "INFINITI": {
        "": 0.93,
        "QX80": 0.96,
        "QX55": 0.94,
        "QX60": 0.93,
    },
    "VOLVO": {
        "": 0.94,
        "XC90": 0.95,
        "XC40": 0.95,
        "XC60": 0.94,
        "C40": 0.93,
    },
    "MINI": {
        "": 0.92,
        "COUNTRYMAN": 0.93,
        "COOPER": 0.92,
    },
    "GENESIS": {
        "": 0.93,
        "GV80": 0.96,
        "GV70": 0.95,
        "G80": 0.93,
    },
}


def _retention_factor(make: str, model: str) -> tuple[float, str]:
    """Return (factor, human-readable label) for the given make/model.

    Uses a two-level lookup: specific model fragment first, then make default.
    Model matching is a prefix match on the upper-cased model string so
    "F-150 SUPERCREW" still matches the "F-150" entry.
    """
    make_map = _RETENTION_PROFILES.get(make)
    if not make_map:
        return 1.00, "Average (unrecognised make)"

    # Try model-specific entry first (longest matching prefix wins)
    best_model_key = ""
    for key in make_map:
        if key and model.startswith(key) and len(key) > len(best_model_key):
            best_model_key = key

    if best_model_key:
        factor = make_map[best_model_key]
        label = f"{make.title()} {best_model_key.title()} — {'holds value well' if factor >= 1.03 else 'depreciates faster than average' if factor < 0.97 else 'average depreciation'}"
        return factor, label

    # Fall back to make-level default
    factor = make_map.get("", 1.00)
    label = f"{make.title()} — {'holds value well' if factor >= 1.03 else 'depreciates faster than average' if factor < 0.97 else 'average depreciation'}"
    return factor, label


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

"""Market data aggregation for valuation inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import csv
from pathlib import Path
from typing import Any

import requests

from .config import AppConfig
from .database import fetch_market_snapshots, upsert_market_snapshot


@dataclass(slots=True)
class MarketSignal:
    source: str
    retail_value: int
    wholesale_value: int
    sample_size: int
    days_supply: int
    confidence: float
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "retail_value": self.retail_value,
            "wholesale_value": self.wholesale_value,
            "sample_size": self.sample_size,
            "days_supply": self.days_supply,
            "confidence": self.confidence,
            "raw": self.raw,
        }


@dataclass(slots=True)
class MarketDataBundle:
    region: str
    auction_value: int
    retail_value: int
    comparable_value: int
    confidence: float
    signals: list[MarketSignal]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "auction_value": self.auction_value,
            "retail_value": self.retail_value,
            "comparable_value": self.comparable_value,
            "confidence": self.confidence,
            "signals": [signal.to_dict() for signal in self.signals],
            "notes": self.notes,
        }


class MarketDataAggregator:
    """Combines licensed feeds, imported snapshots, and demo fallback data."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def fetch_bundle(
        self,
        db_path: Path,
        vehicle: dict[str, Any],
        mileage: int,
        dealer: dict[str, Any] | None = None,
    ) -> MarketDataBundle:
        region = self.config.market_region
        source_settings = _market_source_settings(dealer)
        signals = self._fetch_external_signals(vehicle, mileage, source_settings)
        signals.extend(
            self._fetch_local_signals(db_path, vehicle, region, source_settings)
        )
        notes: list[str] = []
        disabled_sources = _disabled_source_labels(source_settings)
        if disabled_sources:
            notes.append(
                "Admin source toggles disabled: " + ", ".join(disabled_sources) + "."
            )

        if not signals:
            fallback = self._estimate_signal(vehicle, mileage)
            if not source_settings["demo_fallback"]:
                fallback.confidence = 0.35
                notes.append(
                    "No enabled market source returned a match; safety fallback used despite fallback toggle being off."
                )
            else:
                notes.append("No licensed or imported market match found; deterministic demo fallback used.")
            signals.append(fallback)

        auction_signals = [
            signal for signal in signals if "auction" in signal.source or "manheim" in signal.source
        ]
        comparable_signals = [
            signal for signal in signals if signal not in auction_signals
        ] or signals

        auction_value = _weighted_average(
            [(signal.wholesale_value, signal.confidence) for signal in auction_signals or signals]
        )
        retail_value = _weighted_average(
            [(signal.retail_value, signal.confidence) for signal in signals]
        )
        comparable_value = _weighted_average(
            [(signal.retail_value, signal.confidence) for signal in comparable_signals]
        )
        confidence = min(0.98, max(0.35, sum(s.confidence for s in signals) / len(signals)))

        return MarketDataBundle(
            region=region,
            auction_value=auction_value,
            retail_value=retail_value,
            comparable_value=comparable_value,
            confidence=confidence,
            signals=signals,
            notes=notes,
        )

    def _fetch_local_signals(
        self,
        db_path: Path,
        vehicle: dict[str, Any],
        region: str,
        source_settings: dict[str, bool],
    ) -> list[MarketSignal]:
        year = _int_or_none(vehicle.get("year"))
        rows = fetch_market_snapshots(
            db_path,
            make=vehicle.get("make", ""),
            model=vehicle.get("model", ""),
            year=year,
            region=region,
        )
        return [
            MarketSignal(
                source=row["source"],
                retail_value=int(row["retail_value"]),
                wholesale_value=int(row["wholesale_value"]),
                sample_size=int(row["sample_size"]),
                days_supply=int(row["days_supply"]),
                confidence=float(row["confidence"]),
                raw=_safe_json(row.get("raw_json")),
            )
            for row in rows
            if _is_local_source_enabled(row["source"], source_settings)
        ]

    def _fetch_external_signals(
        self,
        vehicle: dict[str, Any],
        mileage: int,
        source_settings: dict[str, bool],
    ) -> list[MarketSignal]:
        signals: list[MarketSignal] = []
        for provider in [
            (
                "manheim_mmr",
                self.config.manheim_api_base,
                self.config.manheim_api_key,
                "auction",
                source_settings["manheim"],
            ),
            (
                "jd_power",
                self.config.jd_power_api_base,
                self.config.jd_power_api_key,
                "retail",
                source_settings["jd_power"],
            ),
            (
                "black_book",
                self.config.black_book_api_base,
                self.config.black_book_api_key,
                "retail",
                source_settings["black_book"],
            ),
            (
                "kelley_blue_book",
                self.config.kbb_api_base,
                self.config.kbb_api_key,
                "kbb",
                source_settings["kbb"],
            ),
        ]:
            source, base_url, api_key, category, enabled = provider
            if not enabled or not base_url or not api_key:
                continue
            signal = self._fetch_generic_provider(source, base_url, api_key, category, vehicle, mileage)
            if signal:
                signals.append(signal)
        return signals

    def _fetch_generic_provider(
        self,
        source: str,
        base_url: str,
        api_key: str,
        category: str,
        vehicle: dict[str, Any],
        mileage: int,
    ) -> MarketSignal | None:
        try:
            response = requests.get(
                f"{base_url.rstrip('/')}/valuation",
                params={
                    "vin": vehicle.get("vin", ""),
                    "year": vehicle.get("year", ""),
                    "make": vehicle.get("make", ""),
                    "model": vehicle.get("model", ""),
                    "trim": vehicle.get("trim", ""),
                    "mileage": mileage,
                    "region": self.config.market_region,
                },
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None

        retail, wholesale = _extract_market_values(payload, category)
        if not retail and wholesale:
            retail = int(wholesale / 0.82)
        if not wholesale and retail:
            wholesale = int(retail * 0.82)
        if not retail or not wholesale:
            return None
        confidence = float(payload.get("confidence") or _default_confidence(category))
        return MarketSignal(
            source=source,
            retail_value=retail,
            wholesale_value=wholesale,
            sample_size=int(payload.get("sample_size") or 0),
            days_supply=int(payload.get("days_supply") or 0),
            confidence=confidence,
            raw=payload,
        )

    def _estimate_signal(self, vehicle: dict[str, Any], mileage: int) -> MarketSignal:
        current_year = datetime.now(timezone.utc).year
        year = _int_or_none(vehicle.get("year")) or max(2016, current_year - 5)
        age = max(0, current_year - year)
        make_factor = _make_factor(vehicle.get("make", ""))
        class_factor = _class_factor(vehicle.get("body_style", ""), vehicle.get("model", ""))
        base_msrp = int((31500 * make_factor * class_factor) / 100) * 100
        depreciation = min(0.72, 0.11 * age + 0.018 * max(age - 3, 0))
        mileage_adjustment = max(-4500, min(4500, (age * 12000 - mileage) * 0.045))
        retail = max(4500, int((base_msrp * (1 - depreciation) + mileage_adjustment) / 100) * 100)
        wholesale = int(retail * 0.79)
        return MarketSignal(
            source="deterministic_demo_estimate",
            retail_value=retail,
            wholesale_value=wholesale,
            sample_size=0,
            days_supply=0,
            confidence=0.48,
            raw={"base_msrp": base_msrp, "age": age, "mileage": mileage},
        )


def import_market_csv(db_path: Path, csv_path: Path, region: str) -> int:
    """Import dealer-owned market snapshots from a CSV file.

    Expected columns: year, make, model, trim, source, retail_value,
    wholesale_value, sample_size, days_supply, confidence.
    """

    count = 0
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if not row.get("make") or not row.get("model"):
                continue
            upsert_market_snapshot(
                db_path,
                {
                    "year": _int_or_none(row.get("year")),
                    "make": row["make"],
                    "model": row["model"],
                    "trim": row.get("trim", ""),
                    "region": row.get("region") or region,
                    "source": row.get("source") or "dealer_csv",
                    "retail_value": row["retail_value"],
                    "wholesale_value": row["wholesale_value"],
                    "sample_size": row.get("sample_size") or 0,
                    "days_supply": row.get("days_supply") or 0,
                    "confidence": row.get("confidence") or 0.7,
                    "captured_at": row.get("captured_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "raw": {"csv_file": str(csv_path)},
                },
            )
            count += 1
    return count


def _weighted_average(values: list[tuple[int, float]]) -> int:
    if not values:
        return 0
    numerator = sum(value * max(weight, 0.1) for value, weight in values)
    denominator = sum(max(weight, 0.1) for _, weight in values)
    return int(round(numerator / denominator / 50) * 50)


def _market_source_settings(dealer: dict[str, Any] | None) -> dict[str, bool]:
    dealer = dealer or {}
    return {
        "manheim": _enabled(dealer, "market_source_manheim_enabled"),
        "jd_power": _enabled(dealer, "market_source_jd_power_enabled"),
        "black_book": _enabled(dealer, "market_source_black_book_enabled"),
        "kbb": _enabled(dealer, "market_source_kbb_enabled"),
        "dealer_import": _enabled(dealer, "market_source_dealer_import_enabled"),
        "demo_fallback": _enabled(dealer, "market_source_demo_fallback_enabled"),
    }


def _enabled(dealer: dict[str, Any], key: str) -> bool:
    return bool(int(dealer.get(key, 1)))


def _is_local_source_enabled(source: str, source_settings: dict[str, bool]) -> bool:
    return source_settings.get(_source_key(source), True)


def _source_key(source: str) -> str:
    source = source.lower()
    if "kbb" in source or "kelley" in source:
        return "kbb"
    if "manheim" in source or "mmr" in source:
        return "manheim"
    if "jd_power" in source or "j.d" in source or "chrome" in source:
        return "jd_power"
    if "black_book" in source or "black book" in source:
        return "black_book"
    return "dealer_import"


def _disabled_source_labels(source_settings: dict[str, bool]) -> list[str]:
    labels = {
        "manheim": "Manheim/MMR",
        "jd_power": "J.D. Power",
        "black_book": "Black Book",
        "kbb": "Kelley Blue Book",
        "dealer_import": "dealer/imported snapshots",
        "demo_fallback": "deterministic fallback",
    }
    return [label for key, label in labels.items() if not source_settings[key]]


def _extract_market_values(payload: dict[str, Any], category: str) -> tuple[int, int]:
    retail_aliases = [
        "retail_value",
        "retail",
        "dealer_retail_value",
        "typical_listing_value",
        "typical_listing_price",
        "fair_market_value",
        "fair_market_range_midpoint",
        "private_party_value",
        "private_party",
    ]
    wholesale_aliases = [
        "wholesale_value",
        "wholesale",
        "auction_value",
        "trade",
        "trade_value",
        "trade_in_value",
        "trade_in",
        "kbb_trade_in_value",
        "lending_value",
    ]
    if category == "kbb":
        retail_aliases = [
            "typical_listing_value",
            "typical_listing_price",
            "fair_market_value",
            "fair_market_range_midpoint",
            "retail_value",
            "retail",
            "private_party_value",
        ]
        wholesale_aliases = [
            "trade_in_value",
            "trade_value",
            "kbb_trade_in_value",
            "auction_value",
            "lending_value",
            "wholesale_value",
            "wholesale",
        ]
    return _first_int(payload, retail_aliases), _first_int(payload, wholesale_aliases)


def _first_int(payload: dict[str, Any], keys: list[str]) -> int:
    for key in keys:
        value = _int_or_none(payload.get(key))
        if value:
            return value
    for container_key in ["values", "valuation", "data", "result"]:
        nested = payload.get(container_key)
        if not isinstance(nested, dict):
            continue
        for key in keys:
            value = _int_or_none(nested.get(key))
            if value:
                return value
    return 0


def _default_confidence(category: str) -> float:
    if category == "auction":
        return 0.88
    if category == "kbb":
        return 0.84
    return 0.78


def _int_or_none(value: object) -> int | None:
    if isinstance(value, dict):
        value = value.get("amount") or value.get("value") or value.get("midpoint")
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _safe_json(value: object) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        import json

        payload = json.loads(str(value))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _make_factor(make: str) -> float:
    premium = {"BMW": 1.32, "MERCEDES": 1.34, "MERCEDES-BENZ": 1.34, "LEXUS": 1.24, "AUDI": 1.24}
    reliable = {"TOYOTA": 1.05, "HONDA": 1.03, "SUBARU": 1.01}
    make_norm = make.strip().upper()
    return premium.get(make_norm, reliable.get(make_norm, 1.0))


def _class_factor(body_style: str, model: str) -> float:
    text = f"{body_style} {model}".upper()
    if any(term in text for term in {"TRUCK", "PICKUP", "F-150", "SILVERADO", "RAM"}):
        return 1.27
    if any(term in text for term in {"SUV", "UTILITY", "RAV4", "CR-V", "EXPLORER"}):
        return 1.12
    if any(term in text for term in {"COUPE", "CONVERTIBLE"}):
        return 1.08
    return 1.0

"""VIN decoding clients."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import requests


VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)


@dataclass(slots=True)
class VinDecodeResult:
    vin: str
    year: int | None
    make: str
    model: str
    trim: str
    body_style: str
    vehicle_type: str
    engine: str
    transmission: str
    source: str
    raw: dict[str, Any]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "vin": self.vin,
            "year": self.year,
            "make": self.make,
            "model": self.model,
            "trim": self.trim,
            "body_style": self.body_style,
            "vehicle_type": self.vehicle_type,
            "engine": self.engine,
            "transmission": self.transmission,
            "source": self.source,
            "raw": self.raw,
            "errors": self.errors,
        }


class VinDecodeError(ValueError):
    """Raised when a VIN cannot be decoded."""


class NhtsaVinDecoder:
    """Client for the public NHTSA vPIC DecodeVinValues API."""

    def __init__(self, api_base: str, timeout_seconds: float = 8) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def decode(self, vin: str) -> VinDecodeResult:
        normalized = normalize_vin(vin)
        url = f"{self.api_base}/vehicles/DecodeVinValues/{normalized}"
        response = requests.get(
            url,
            params={"format": "json"},
            timeout=self.timeout_seconds,
            headers={"User-Agent": "AutoTradePro/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("Results") or []
        if not results:
            raise VinDecodeError("NHTSA returned no VIN decode results.")
        result = results[0]
        error_text = str(result.get("ErrorText") or "").strip()
        serious_errors = [
            part.strip()
            for part in error_text.split(";")
            if part.strip() and not part.strip().startswith("0 -")
        ]
        year = _int_or_none(result.get("ModelYear"))
        return VinDecodeResult(
            vin=normalized,
            year=year,
            make=_clean(result.get("Make")),
            model=_clean(result.get("Model")),
            trim=_clean(result.get("Trim") or result.get("Series")),
            body_style=_clean(result.get("BodyClass")),
            vehicle_type=_clean(result.get("VehicleType")),
            engine=_engine_description(result),
            transmission=_clean(result.get("TransmissionStyle")),
            source="nhtsa_vpic",
            raw=result,
            errors=serious_errors,
        )


def normalize_vin(vin: str) -> str:
    normalized = re.sub(r"\s+", "", vin or "").upper()
    if not VIN_RE.match(normalized):
        raise VinDecodeError("VIN must be 17 characters and cannot include I, O, or Q.")
    return normalized


def fallback_decode(vin: str) -> VinDecodeResult:
    """Return a valid placeholder when remote decoding is unavailable."""

    normalized = normalize_vin(vin)
    return VinDecodeResult(
        vin=normalized,
        year=None,
        make="",
        model="",
        trim="",
        body_style="",
        vehicle_type="",
        engine="",
        transmission="",
        source="manual_entry",
        raw={},
        errors=["VIN format is valid, but live decoding was unavailable."],
    )


def _clean(value: object) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"not applicable", "null", "none"} else text


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _engine_description(result: dict[str, Any]) -> str:
    parts = [
        result.get("EngineConfiguration"),
        result.get("EngineCylinders") and f"{result.get('EngineCylinders')} cyl",
        result.get("DisplacementL") and f"{result.get('DisplacementL')}L",
        result.get("FuelTypePrimary"),
    ]
    return ", ".join(_clean(part) for part in parts if _clean(part))

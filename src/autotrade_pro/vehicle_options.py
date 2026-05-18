"""Vehicle option catalog helpers for public trade-in selectors."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests


MIN_MODEL_YEAR = 1981
VEHICLE_TYPES = ("car", "truck", "multipurpose passenger vehicle")
BODY_STYLES = [
    "Sedan",
    "Coupe",
    "Convertible",
    "Hatchback",
    "Wagon",
    "SUV",
    "Crossover",
    "Pickup",
    "Minivan",
    "Van",
    "EV Sedan",
    "EV Hatchback",
    "Hybrid Hatchback",
    "Luxury Sedan",
    "Sports Car",
]

FALLBACK_MODELS_BY_MAKE = {
    "ACURA": ["Integra", "MDX", "RDX", "TLX"],
    "AUDI": ["A3", "A4", "A5", "A6", "Q3", "Q5", "Q7", "Q8", "e-tron"],
    "BMW": ["2 Series", "3 Series", "4 Series", "5 Series", "X1", "X3", "X5", "X7", "i4"],
    "BUICK": ["Encore", "Envision", "Enclave"],
    "CADILLAC": ["CT4", "CT5", "Escalade", "XT4", "XT5", "XT6", "Lyriq"],
    "CHEVROLET": ["Blazer", "Bolt", "Camaro", "Colorado", "Corvette", "Equinox", "Malibu", "Silverado", "Suburban", "Tahoe", "Traverse"],
    "CHRYSLER": ["300", "Pacifica", "Voyager"],
    "DODGE": ["Challenger", "Charger", "Durango", "Grand Caravan", "Hornet"],
    "FORD": ["Bronco", "Edge", "Escape", "Expedition", "Explorer", "F-150", "Maverick", "Mustang", "Ranger", "Transit"],
    "GENESIS": ["G70", "G80", "G90", "GV70", "GV80"],
    "GMC": ["Acadia", "Canyon", "Sierra", "Terrain", "Yukon"],
    "HONDA": ["Accord", "Civic", "CR-V", "Fit", "HR-V", "Odyssey", "Passport", "Pilot", "Ridgeline"],
    "HYUNDAI": ["Elantra", "Ioniq 5", "Kona", "Palisade", "Santa Fe", "Sonata", "Tucson"],
    "INFINITI": ["Q50", "Q60", "QX50", "QX60", "QX80"],
    "JEEP": ["Cherokee", "Compass", "Gladiator", "Grand Cherokee", "Renegade", "Wagoneer", "Wrangler"],
    "KIA": ["Carnival", "EV6", "Forte", "K5", "Niro", "Seltos", "Sorento", "Soul", "Sportage", "Telluride"],
    "LEXUS": ["ES", "GX", "IS", "LC", "LS", "LX", "NX", "RX", "TX", "UX"],
    "LINCOLN": ["Aviator", "Corsair", "Nautilus", "Navigator"],
    "MAZDA": ["CX-30", "CX-5", "CX-50", "CX-9", "CX-90", "Mazda3", "MX-5 Miata"],
    "MERCEDES-BENZ": ["A-Class", "C-Class", "E-Class", "GLA", "GLC", "GLE", "GLS", "S-Class"],
    "MINI": ["Clubman", "Convertible", "Cooper", "Countryman", "Hardtop"],
    "NISSAN": ["Altima", "Armada", "Frontier", "Leaf", "Maxima", "Murano", "Pathfinder", "Rogue", "Sentra", "Titan", "Versa"],
    "PORSCHE": ["718", "911", "Cayenne", "Macan", "Panamera", "Taycan"],
    "RAM": ["1500", "2500", "3500", "ProMaster"],
    "SUBARU": ["Ascent", "BRZ", "Crosstrek", "Forester", "Impreza", "Legacy", "Outback", "WRX"],
    "TESLA": ["Cybertruck", "Model 3", "Model S", "Model X", "Model Y"],
    "TOYOTA": ["4Runner", "Camry", "Corolla", "Highlander", "Prius", "RAV4", "Sequoia", "Sienna", "Tacoma", "Tundra"],
    "VOLKSWAGEN": ["Atlas", "Golf", "ID.4", "Jetta", "Passat", "Taos", "Tiguan"],
    "VOLVO": ["S60", "S90", "V60", "XC40", "XC60", "XC90"],
}

GENERIC_TRIMS = [
    "Base",
    "S",
    "SE",
    "SEL",
    "Sport",
    "Premium",
    "Limited",
    "Touring",
    "Luxury",
    "Platinum",
]

MODEL_TRIMS = {
    "ACCORD": ["LX", "Sport", "EX", "EX-L", "Touring", "Hybrid Sport", "Hybrid Touring"],
    "CAMRY": ["LE", "SE", "XLE", "XSE", "TRD", "Hybrid LE", "Hybrid XLE"],
    "CIVIC": ["LX", "Sport", "EX", "EX-L", "Touring", "Si", "Type R"],
    "COROLLA": ["L", "LE", "SE", "XLE", "XSE", "Hybrid LE"],
    "CR-V": ["LX", "EX", "EX-L", "Sport", "Sport-L", "Touring"],
    "F-150": ["XL", "XLT", "Lariat", "King Ranch", "Platinum", "Limited", "Raptor"],
    "MODEL 3": ["Rear-Wheel Drive", "Long Range", "Performance"],
    "MODEL Y": ["Rear-Wheel Drive", "Long Range", "Performance"],
    "RAV4": ["LE", "XLE", "XLE Premium", "Adventure", "Limited", "Hybrid XLE", "Prime XSE"],
    "SILVERADO": ["WT", "Custom", "LT", "RST", "LTZ", "High Country", "ZR2"],
    "TACOMA": ["SR", "SR5", "TRD Sport", "TRD Off-Road", "Limited", "TRD Pro"],
    "TELLURIDE": ["LX", "S", "EX", "SX", "SX Prestige", "X-Line"],
    "WRANGLER": ["Sport", "Willys", "Sahara", "Rubicon", "High Altitude"],
}

_CACHE: dict[str, Any] = {}


class VehicleOptionsClient:
    """NHTSA-backed vehicle selector data with deterministic local fallback."""

    def __init__(self, api_base: str, timeout_seconds: float = 8) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def years(self) -> list[int]:
        current_year = datetime.now(timezone.utc).year + 1
        return list(range(current_year, MIN_MODEL_YEAR - 1, -1))

    def makes(self) -> tuple[list[str], str]:
        cache_key = f"makes:{self.api_base}"
        if cache_key in _CACHE:
            return _CACHE[cache_key], "cache"

        makes: set[str] = set()
        try:
            for vehicle_type in VEHICLE_TYPES:
                payload = self._get(
                    f"vehicles/GetMakesForVehicleType/{vehicle_type}",
                    params={"format": "json"},
                )
                for row in payload.get("Results") or []:
                    make = _clean(row.get("MakeName") or row.get("Make_Name"))
                    if make:
                        makes.add(make.upper())
            if makes:
                result = sorted(makes)
                _CACHE[cache_key] = result
                return result, "nhtsa_vpic"
        except Exception:
            pass

        result = sorted(FALLBACK_MODELS_BY_MAKE)
        return result, "fallback"

    def models(self, make: str, year: int | None = None) -> tuple[list[str], str]:
        make = _clean(make).upper()
        if not make:
            return [], "empty"

        cache_key = f"models:{self.api_base}:{year or 'all'}:{make}"
        if cache_key in _CACHE:
            return _CACHE[cache_key], "cache"

        try:
            path = f"vehicles/GetModelsForMakeYear/make/{make}"
            if year:
                path = f"{path}/modelyear/{year}"
            payload = self._get(path, params={"format": "json"})
            models = sorted(
                {
                    _clean(row.get("Model_Name") or row.get("ModelName"))
                    for row in payload.get("Results") or []
                    if _clean(row.get("Model_Name") or row.get("ModelName"))
                },
                key=str.upper,
            )
            if models:
                _CACHE[cache_key] = models
                return models, "nhtsa_vpic"
        except Exception:
            pass

        return FALLBACK_MODELS_BY_MAKE.get(make, []), "fallback"

    def trims(self, make: str, model: str) -> list[str]:
        model_key = _clean(model).upper()
        trims = MODEL_TRIMS.get(model_key, [])
        return sorted(set([*trims, *GENERIC_TRIMS]), key=str.upper)

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        response = requests.get(
            f"{self.api_base}/{path.lstrip('/')}",
            params=params,
            timeout=self.timeout_seconds,
            headers={"User-Agent": "AutoTradePro/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}


def _clean(value: object) -> str:
    return str(value or "").strip()

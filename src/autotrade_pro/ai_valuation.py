"""Optional OpenAI-powered trade-in review and photo digest."""

from __future__ import annotations

import base64
import json
import math
from typing import Any

import requests
from werkzeug.datastructures import FileStorage

from .market_data import MarketDataBundle
from .valuation import ValuationResult


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
MAX_IMAGES = 5
MAX_IMAGE_BYTES = 4 * 1024 * 1024


AI_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "suggested_trade_offer": {"type": "integer"},
        "suggested_low": {"type": "integer"},
        "suggested_high": {"type": "integer"},
        "confidence": {"type": "number"},
        "apply_adjustment": {"type": "boolean"},
        "price_rationale": {"type": "string"},
        "value_consistency_notes": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "image_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "observations": {"type": "string"},
                    "damage_detected": {"type": "boolean"},
                    "severity": {
                        "type": "string",
                        "enum": ["none", "minor", "moderate", "major", "unknown"],
                    },
                    "estimated_reconditioning_impact": {"type": "integer"},
                },
                "required": [
                    "label",
                    "observations",
                    "damage_detected",
                    "severity",
                    "estimated_reconditioning_impact",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "suggested_trade_offer",
        "suggested_low",
        "suggested_high",
        "confidence",
        "apply_adjustment",
        "price_rationale",
        "value_consistency_notes",
        "risk_flags",
        "image_findings",
    ],
    "additionalProperties": False,
}


def request_ai_trade_review(
    *,
    dealer: dict[str, Any],
    vehicle: dict[str, Any],
    mileage: int,
    condition_answers: dict[str, Any],
    photo_files: list[FileStorage],
    photo_labels: list[str],
    market: MarketDataBundle,
    result: ValuationResult,
) -> dict[str, Any] | None:
    if not _enabled(dealer):
        return None
    api_key = str(dealer.get("openai_api_key") or "").strip()
    if not api_key:
        return None

    model = str(dealer.get("openai_model") or "gpt-4.1-mini").strip()
    image_parts = (
        _image_parts(photo_files, photo_labels)
        if _bool(dealer.get("openai_image_analysis_enabled"), default=True)
        else []
    )
    image_count = sum(1 for part in image_parts if part.get("type") == "input_image")
    prompt = _prompt(vehicle, mileage, condition_answers, market, result, bool(image_parts))
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You are a dealership trade-in valuation reviewer. "
                    "Use the provided market numbers, deterministic offer, condition answers, "
                    "and vehicle photos to catch obvious pricing issues and summarize photo condition. "
                    "Do not identify people or infer sensitive personal traits. "
                    "Return only the requested structured JSON."
                ),
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}, *image_parts],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "trade_in_review",
                "strict": True,
                "schema": AI_REVIEW_SCHEMA,
            }
        },
        "max_output_tokens": 900,
    }
    try:
        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        parsed = _extract_json(data)
        if isinstance(parsed, dict):
            parsed["model"] = model
            parsed["image_count"] = image_count
            return parsed
    except Exception as exc:
        return {
            "error": str(exc),
            "model": model,
            "image_count": image_count,
            "applied": False,
        }
    return None


def apply_ai_trade_review(
    result: ValuationResult, review: dict[str, Any] | None, dealer: dict[str, Any]
) -> ValuationResult:
    if not review:
        return result

    review_record = _public_review_record(review)
    result.source_breakdown["ai_review"] = review_record
    result.adjustments["ai_review"] = review_record

    if review.get("error"):
        return result

    confidence = _float(review.get("confidence"))
    suggested = _round_to_50(_float(review.get("suggested_trade_offer")))
    apply_adjustment = bool(review.get("apply_adjustment"))
    if not apply_adjustment or confidence < 0.55 or suggested < 500:
        review_record["applied"] = False
        return result

    limit_percent = _float(dealer.get("openai_price_adjustment_limit_percent")) or 0.06
    max_delta = max(500, result.trade_offer * max(0.0, min(limit_percent, 0.15)))
    lower_bound = result.trade_offer - max_delta
    upper_bound = result.trade_offer + max_delta
    adjusted_offer = _round_to_50(
        max(500, max(lower_bound, min(suggested, result.cap_value, upper_bound)))
    )
    review_record.update(
        {
            "applied": adjusted_offer != result.trade_offer,
            "pre_ai_trade_offer": result.trade_offer,
            "adjustment_limit_percent": limit_percent,
            "adjusted_trade_offer": adjusted_offer,
        }
    )
    result.trade_offer = adjusted_offer
    result.valuation_low = max(500, _round_to_50(adjusted_offer * 0.97))
    result.valuation_high = min(result.cap_value, _round_to_50(adjusted_offer * 1.03))
    return result


def attach_ai_photo_findings(
    photo_summary: list[dict[str, Any]], review: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if not review or not isinstance(review.get("image_findings"), list):
        return photo_summary
    findings = {
        str(item.get("label", "")).strip().lower(): item
        for item in review.get("image_findings", [])
        if isinstance(item, dict)
    }
    for photo in photo_summary:
        finding = findings.get(str(photo.get("label", "")).strip().lower())
        if finding:
            photo["ai_findings"] = {
                key: finding.get(key)
                for key in [
                    "observations",
                    "damage_detected",
                    "severity",
                    "estimated_reconditioning_impact",
                ]
            }
    return photo_summary


def _prompt(
    vehicle: dict[str, Any],
    mileage: int,
    condition_answers: dict[str, Any],
    market: MarketDataBundle,
    result: ValuationResult,
    has_images: bool,
) -> str:
    payload = {
        "vehicle": vehicle,
        "mileage": mileage,
        "condition_answers": condition_answers,
        "market_values": market.to_dict(),
        "deterministic_result": result.to_dict(),
        "review_rules": {
            "respect_cap_value": result.cap_value,
            "prefer_small_adjustments": True,
            "image_review_enabled": has_images,
            "output_rounding": "nearest 50 dollars",
        },
    }
    return (
        "Review this trade-in valuation for pricing sanity. "
        "Suggest an offer only if the deterministic offer appears too high or too low "
        "given the market, mileage, condition answers, and visible vehicle condition. "
        "If photos are present, digest visible damage, warning-light clues, tire/brake clues, "
        "interior wear, and anything that could affect reconditioning cost. "
        f"Input JSON:\n{json.dumps(payload, separators=(',', ':'), sort_keys=True)}"
    )


def _image_parts(files: list[FileStorage], labels: list[str]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for index, file in enumerate(files[:MAX_IMAGES]):
        if not file or not file.filename:
            continue
        content_type = file.content_type or "application/octet-stream"
        if not content_type.startswith("image/"):
            continue
        data = _read_file_bytes(file)
        if not data or len(data) > MAX_IMAGE_BYTES:
            continue
        label = labels[index] if index < len(labels) else f"photo_{index + 1}"
        parts.append(
            {
                "type": "input_image",
                "image_url": f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}",
                "detail": "low",
            }
        )
        parts.append({"type": "input_text", "text": f"Previous image label: {label}"})
    return parts


def _read_file_bytes(file: FileStorage) -> bytes:
    stream = file.stream
    position = None
    try:
        position = stream.tell()
    except Exception:
        position = None
    try:
        data = stream.read()
    finally:
        try:
            stream.seek(position or 0)
        except Exception:
            pass
    return data or b""


def _extract_json(data: dict[str, Any]) -> dict[str, Any] | None:
    text = data.get("output_text")
    if not text:
        chunks: list[str] = []
        for output in data.get("output") or []:
            for content in output.get("content") or []:
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                    chunks.append(str(content.get("text") or ""))
        text = "\n".join(chunks)
    if not text:
        return None
    parsed = json.loads(str(text))
    return parsed if isinstance(parsed, dict) else None


def _public_review_record(review: dict[str, Any]) -> dict[str, Any]:
    return {
        key: review.get(key)
        for key in [
            "model",
            "image_count",
            "suggested_trade_offer",
            "suggested_low",
            "suggested_high",
            "confidence",
            "apply_adjustment",
            "price_rationale",
            "value_consistency_notes",
            "risk_flags",
            "image_findings",
            "error",
        ]
        if key in review
    }


def _enabled(dealer: dict[str, Any]) -> bool:
    return _bool(dealer.get("openai_valuation_enabled"))


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _float(value: object) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(number) or math.isinf(number) else number


def _round_to_50(value: float) -> int:
    return int(round(value / 50) * 50)

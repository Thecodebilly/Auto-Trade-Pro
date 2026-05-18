"""HTTP routes for AutoTrade Pro."""

from __future__ import annotations

import csv
from datetime import datetime
from functools import wraps
from io import StringIO
import json
from pathlib import Path
import secrets
from typing import Any, Callable

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from .config import AppConfig
from .crm import emit_crm_event
from .database import (
    add_vehicle_photo,
    create_customer_and_appointment,
    create_valuation,
    fetch_dashboard_stats,
    fetch_dealer_by_slug,
    fetch_first_dealer,
    fetch_valuation_by_public_id,
    list_crm_events,
    list_data_source_status,
    list_dealer_leads,
    list_dealers,
    list_incentives,
    update_dealer,
    update_incentive,
)
from .market_data import MarketDataAggregator
from .notifications import send_confirmation
from .valuation import build_valuation_record, calculate_valuation
from .vin import NhtsaVinDecoder, VinDecodeError, fallback_decode, normalize_vin
from .workers import refresh_market_data_once


def create_blueprint(config: AppConfig) -> Blueprint:
    bp = Blueprint("autotrade", __name__)

    @bp.get("/healthz")
    def healthz() -> Response:
        return jsonify({"ok": True})

    @bp.app_template_filter("currency")
    def currency(value: object) -> str:
        try:
            return f"${int(float(value)):,}"
        except (TypeError, ValueError):
            return "$0"

    @bp.get("/")
    def index() -> Response:
        dealer = fetch_dealer_by_slug(config.database_path, config.default_dealer_slug)
        dealer = dealer or fetch_first_dealer(config.database_path)
        if dealer is None:
            abort(500)
        return redirect(url_for("autotrade.public_app", dealer_slug=dealer["slug"]))

    @bp.get("/d/<dealer_slug>")
    def public_app(dealer_slug: str) -> str:
        dealer = _dealer_or_404(config, dealer_slug)
        incentives = list_incentives(config.database_path, dealer["id"])
        return render_template("public.html", dealer=dealer, incentives=incentives)

    @bp.get("/api/dealers/<dealer_slug>/config")
    def dealer_config(dealer_slug: str) -> Response:
        dealer = _dealer_or_404(config, dealer_slug)
        return jsonify({"dealer": _dealer_public_payload(dealer), "incentives": list_incentives(config.database_path, dealer["id"])})

    @bp.post("/api/dealers/<dealer_slug>/decode-vin")
    def decode_vin(dealer_slug: str) -> Response:
        _dealer_or_404(config, dealer_slug)
        payload = request.get_json(silent=True) or {}
        vin = payload.get("vin") or request.form.get("vin") or ""
        try:
            decoder = NhtsaVinDecoder(config.nhtsa_api_base, config.nhtsa_timeout_seconds)
            decoded = decoder.decode(vin)
        except VinDecodeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            try:
                decoded = fallback_decode(vin)
            except VinDecodeError as vin_exc:
                return jsonify({"ok": False, "error": str(vin_exc)}), 400
            decoded.errors.append(f"Live NHTSA decode failed: {exc}")
        return jsonify({"ok": True, "vehicle": decoded.to_dict()})

    @bp.post("/api/dealers/<dealer_slug>/valuations")
    def submit_valuation(dealer_slug: str) -> Response:
        dealer = _dealer_or_404(config, dealer_slug)
        try:
            vin = normalize_vin(_field("vin"))
            mileage = int(_field("mileage"))
            if mileage < 0:
                raise ValueError
        except (VinDecodeError, ValueError):
            return jsonify({"ok": False, "error": "Enter a valid VIN and mileage."}), 400

        vehicle = _json_field("vehicle_json", {})
        vehicle.update({key: value for key, value in {"vin": vin}.items() if value})
        condition_answers = _json_field("condition_json", {})
        photo_labels = _json_field("photo_labels_json", [])
        files = request.files.getlist("photos")
        sanitized_labels = _labels_for_files(photo_labels, len(files))

        if not vehicle.get("make") or not vehicle.get("model"):
            try:
                decoded = NhtsaVinDecoder(config.nhtsa_api_base, config.nhtsa_timeout_seconds).decode(vin)
                vehicle.update({key: value for key, value in decoded.to_dict().items() if value and key != "raw"})
            except Exception:
                pass

        aggregator = MarketDataAggregator(config)
        market = aggregator.fetch_bundle(config.database_path, vehicle, mileage)
        result = calculate_valuation(
            dealer=dealer,
            vehicle=vehicle,
            mileage=mileage,
            condition_answers=condition_answers,
            photo_labels=sanitized_labels,
            market=market,
        )
        photo_summary = _build_photo_summary(files, sanitized_labels)
        record = build_valuation_record(
            dealer=dealer,
            vehicle=vehicle,
            mileage=mileage,
            condition_answers=condition_answers,
            photo_summary=photo_summary,
            market=market,
            result=result,
        )
        valuation_id = create_valuation(config.database_path, record)
        _save_photos(config.uploads_path, config.database_path, result.public_id, valuation_id, files, sanitized_labels)
        emit_crm_event(
            config.database_path,
            config,
            dealer,
            valuation_id,
            "valuation.created",
            {"valuation": result.to_dict(), "vehicle": vehicle, "mileage": mileage},
        )
        return jsonify(
            {
                "ok": True,
                "valuation": {
                    **result.to_dict(),
                    "vehicle": vehicle,
                    "dealer": _dealer_public_payload(dealer),
                    "incentives": list_incentives(config.database_path, dealer["id"]),
                },
            }
        )

    @bp.get("/api/valuations/<public_id>")
    def valuation_status(public_id: str) -> Response:
        valuation = fetch_valuation_by_public_id(config.database_path, public_id)
        if valuation is None:
            return jsonify({"ok": False, "error": "Valuation not found."}), 404
        return jsonify({"ok": True, "valuation": _valuation_payload(valuation)})

    @bp.post("/api/valuations/<public_id>/appointments")
    def book_appointment(public_id: str) -> Response:
        valuation = fetch_valuation_by_public_id(config.database_path, public_id)
        if valuation is None:
            return jsonify({"ok": False, "error": "Valuation not found."}), 404
        payload = request.get_json(silent=True) or {}
        customer = {
            "name": str(payload.get("name", "")).strip(),
            "email": str(payload.get("email", "")).strip(),
            "phone": str(payload.get("phone", "")).strip(),
            "marketing_consent": bool(payload.get("marketing_consent")),
        }
        appointment = {
            "scheduled_date": str(payload.get("scheduled_date", "")).strip(),
            "scheduled_time": str(payload.get("scheduled_time", "")).strip(),
            "notes": str(payload.get("notes", "")).strip(),
            "confirmation_code": f"AT-{secrets.token_hex(3).upper()}",
        }
        if not all([customer["name"], customer["email"], customer["phone"], appointment["scheduled_date"], appointment["scheduled_time"]]):
            return jsonify({"ok": False, "error": "Contact details and appointment time are required."}), 400
        create_customer_and_appointment(
            config.database_path,
            valuation_id=valuation["id"],
            dealer_id=valuation["dealer_id"],
            customer=customer,
            appointment=appointment,
        )
        updated = fetch_valuation_by_public_id(config.database_path, public_id)
        dealer = fetch_dealer_by_slug(config.database_path, updated["dealer_slug"])
        emit_crm_event(
            config.database_path,
            config,
            dealer,
            valuation["id"],
            "appointment.booked",
            {"valuation": _valuation_payload(updated), "customer": customer, "appointment": appointment},
        )
        notification_notes = send_confirmation(config, updated)
        return jsonify(
            {
                "ok": True,
                "valuation": _valuation_payload(updated),
                "notification_notes": notification_notes,
            }
        )

    @bp.route("/admin/login", methods=["GET", "POST"])
    def admin_login() -> str | Response:
        error = ""
        if request.method == "POST":
            password = request.form.get("password", "")
            if secrets.compare_digest(password, config.admin_password):
                session["admin"] = True
                return redirect(url_for("autotrade.admin_dashboard"))
            error = "That password did not match."
        return render_template("admin_login.html", error=error)

    @bp.post("/admin/logout")
    def admin_logout() -> Response:
        session.clear()
        return redirect(url_for("autotrade.admin_login"))

    @bp.get("/admin")
    @_require_admin
    def admin_dashboard() -> str:
        dealers = list_dealers(config.database_path)
        selected_slug = request.args.get("dealer") or (dealers[0]["slug"] if dealers else "")
        dealer = fetch_dealer_by_slug(config.database_path, selected_slug) or (dealers[0] if dealers else None)
        if dealer is None:
            abort(500)
        stats = fetch_dashboard_stats(config.database_path, dealer["id"])
        leads = list_dealer_leads(config.database_path, dealer["id"])
        incentives = list_incentives(config.database_path, dealer["id"], active_only=False)
        crm_events = list_crm_events(config.database_path, dealer["id"])
        data_sources = list_data_source_status(config.database_path)
        return render_template(
            "admin.html",
            dealers=dealers,
            dealer=dealer,
            stats=stats,
            leads=leads,
            incentives=incentives,
            crm_events=crm_events,
            data_sources=data_sources,
        )

    @bp.post("/admin/dealer/<int:dealer_id>")
    @_require_admin
    def admin_update_dealer(dealer_id: int) -> Response:
        fields = {
            "name": request.form.get("name", ""),
            "legal_name": request.form.get("legal_name", ""),
            "logo_url": request.form.get("logo_url", ""),
            "hero_image_url": request.form.get("hero_image_url", ""),
            "primary_color": request.form.get("primary_color", "#184E77"),
            "accent_color": request.form.get("accent_color", "#F9A03F"),
            "phone": request.form.get("phone", ""),
            "email": request.form.get("email", ""),
            "address_line1": request.form.get("address_line1", ""),
            "city": request.form.get("city", ""),
            "state": request.form.get("state", ""),
            "postal_code": request.form.get("postal_code", ""),
            "appointment_timezone": request.form.get("appointment_timezone", "America/New_York"),
            "bonus_credit_enabled": 1 if request.form.get("bonus_credit_enabled") else 0,
            "bonus_credit_amount": int(request.form.get("bonus_credit_amount") or 0),
            "valuation_hold_days": int(request.form.get("valuation_hold_days") or 10),
            "max_retail_percent": float(request.form.get("max_retail_percent") or 0.95),
            "crm_webhook_url": request.form.get("crm_webhook_url", ""),
        }
        update_dealer(config.database_path, dealer_id, fields)
        return redirect(url_for("autotrade.admin_dashboard"))

    @bp.post("/admin/incentives/<int:incentive_id>")
    @_require_admin
    def admin_update_incentive(incentive_id: int) -> Response:
        fields = {
            "reveal_step": request.form.get("reveal_step", "offer"),
            "title": request.form.get("title", ""),
            "description": request.form.get("description", ""),
            "value_label": request.form.get("value_label", ""),
            "icon": request.form.get("icon", "gift"),
            "active": 1 if request.form.get("active") else 0,
            "sort_order": int(request.form.get("sort_order") or 0),
        }
        update_incentive(config.database_path, incentive_id, fields)
        return redirect(url_for("autotrade.admin_dashboard"))

    @bp.post("/admin/market-refresh")
    @_require_admin
    def admin_market_refresh() -> Response:
        result = refresh_market_data_once(config)
        return jsonify(result)

    @bp.get("/admin/export.csv")
    @_require_admin
    def admin_export_csv() -> Response:
        dealer = fetch_first_dealer(config.database_path)
        if dealer is None:
            abort(404)
        rows = list_dealer_leads(config.database_path, dealer["id"], limit=5000)
        handle = StringIO()
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["public_id"])
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            handle.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=autotrade_leads.csv"},
        )

    @bp.app_errorhandler(404)
    def not_found(_: Exception) -> tuple[str, int]:
        return render_template("not_found.html"), 404

    return bp


def _dealer_or_404(config: AppConfig, slug: str) -> dict[str, Any]:
    dealer = fetch_dealer_by_slug(config.database_path, slug)
    if dealer is None:
        abort(404)
    return dealer


def _dealer_public_payload(dealer: dict[str, Any]) -> dict[str, Any]:
    return {
        key: dealer.get(key)
        for key in [
            "slug",
            "name",
            "logo_url",
            "hero_image_url",
            "primary_color",
            "accent_color",
            "phone",
            "email",
            "address_line1",
            "city",
            "state",
            "postal_code",
            "bonus_credit_enabled",
            "bonus_credit_amount",
            "valuation_hold_days",
            "max_retail_percent",
        ]
    }


def _valuation_payload(valuation: dict[str, Any]) -> dict[str, Any]:
    payload = dict(valuation)
    for field in [
        "condition_answers_json",
        "photo_summary_json",
        "vehicle_payload_json",
        "source_data_json",
        "adjustments_json",
        "source_breakdown_json",
    ]:
        payload[field.removesuffix("_json")] = _loads(payload.pop(field, "{}"))
    return payload


def _require_admin(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not session.get("admin"):
            return redirect(url_for("autotrade.admin_login"))
        return view(*args, **kwargs)

    return wrapped


def _field(name: str) -> str:
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        return str(payload.get(name, "")).strip()
    return str(request.form.get(name, "")).strip()


def _json_field(name: str, default: Any) -> Any:
    raw: object
    if request.is_json:
        raw = (request.get_json(silent=True) or {}).get(name, default)
    else:
        raw = request.form.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    return _loads(str(raw), default)


def _loads(raw: str, default: Any = None) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default if default is not None else {}


def _labels_for_files(labels: Any, file_count: int) -> list[str]:
    fallback = ["front", "rear", "interior", "dash", "tires"]
    if not isinstance(labels, list):
        labels = []
    normalized = [str(label).strip().lower() for label in labels if str(label).strip()]
    while len(normalized) < file_count:
        normalized.append(fallback[len(normalized)] if len(normalized) < len(fallback) else "other")
    return normalized[:file_count]


def _build_photo_summary(files: list[FileStorage], labels: list[str]) -> list[dict[str, Any]]:
    summary = []
    for index, file in enumerate(files):
        if not file or not file.filename:
            continue
        summary.append(
            {
                "label": labels[index] if index < len(labels) else "other",
                "filename": secure_filename(file.filename),
                "content_type": file.content_type or "application/octet-stream",
            }
        )
    return summary


def _save_photos(
    uploads_path: Path,
    db_path: Path,
    public_id: str,
    valuation_id: int,
    files: list[FileStorage],
    labels: list[str],
) -> None:
    destination = uploads_path / public_id
    destination.mkdir(parents=True, exist_ok=True)
    for index, file in enumerate(files):
        if not file or not file.filename:
            continue
        original = secure_filename(file.filename) or f"vehicle-photo-{index + 1}.jpg"
        storage_name = f"{index + 1:02d}-{secrets.token_hex(4)}-{original}"
        full_path = destination / storage_name
        file.save(full_path)
        add_vehicle_photo(
            db_path,
            valuation_id,
            {
                "label": labels[index] if index < len(labels) else "other",
                "original_filename": original,
                "storage_name": str(full_path.relative_to(uploads_path)),
                "content_type": file.content_type or "application/octet-stream",
                "size_bytes": full_path.stat().st_size,
            },
        )


def _iso_to_display(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%b %-d, %Y")
    except Exception:
        return value

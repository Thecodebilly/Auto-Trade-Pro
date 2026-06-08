"""HTTP routes for AutoTrade Pro."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
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

from .ai_valuation import (
    apply_ai_trade_review,
    attach_ai_photo_findings,
    request_ai_pricing_reasoning,
    request_ai_trade_review,
)
from .config import AppConfig
from .crm import emit_crm_event
from .database import (
    add_vehicle_photo,
    count_inventory_vehicles,
    create_customer_and_appointment,
    create_inventory_source,
    create_valuation,
    delete_inventory_source,
    delete_inventory_vehicle,
    fetch_admin_dashboard_metrics,
    fetch_dashboard_stats,
    fetch_dealer_by_slug,
    fetch_first_dealer,
    fetch_valuation_by_public_id,
    get_inventory_source,
    get_inventory_vehicle,
    list_crm_events,
    list_data_source_status,
    list_dealer_leads,
    list_dealers,
    list_incentives,
    list_inventory_sources,
    list_inventory_vehicles,
    update_dealer,
    update_incentive,
    update_inventory_source_sync_result,
    update_inventory_vehicle,
    upsert_inventory_vehicles,
)
from .market_data import MarketDataAggregator, import_market_csv
from .notifications import send_confirmation
from .trends import build_trade_value_trend
from .valuation import build_valuation_record, calculate_valuation
from .vehicle_options import BODY_STYLES, VehicleOptionsClient
from .vin import NhtsaVinDecoder, VinDecodeError, fallback_decode, normalize_vin
from .workers import refresh_market_data_once
from .inventory_scraper import scrape_inventory


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
        return jsonify(
            {
                "dealer": _dealer_public_payload(dealer),
                "incentives": list_incentives(config.database_path, dealer["id"]),
            }
        )

    @bp.get("/api/vehicle-options")
    def vehicle_options() -> Response:
        client = VehicleOptionsClient(
            config.nhtsa_api_base, config.nhtsa_timeout_seconds
        )
        makes, source = client.makes()
        return jsonify(
            {
                "ok": True,
                "source": source,
                "years": client.years(),
                "makes": makes,
                "body_styles": BODY_STYLES,
            }
        )

    @bp.get("/api/vehicle-options/models")
    def vehicle_models() -> Response:
        make = request.args.get("make", "")
        year = _int_or_none(request.args.get("year"))
        client = VehicleOptionsClient(
            config.nhtsa_api_base, config.nhtsa_timeout_seconds
        )
        models, source = client.models(make, year)
        return jsonify({"ok": True, "source": source, "models": models})

    @bp.get("/api/vehicle-options/trims")
    def vehicle_trims() -> Response:
        make = request.args.get("make", "")
        model = request.args.get("model", "")
        client = VehicleOptionsClient(
            config.nhtsa_api_base, config.nhtsa_timeout_seconds
        )
        return jsonify({"ok": True, "trims": client.trims(make, model)})

    @bp.post("/api/dealers/<dealer_slug>/decode-vin")
    def decode_vin(dealer_slug: str) -> Response:
        _dealer_or_404(config, dealer_slug)
        payload = request.get_json(silent=True) or {}
        vin = payload.get("vin") or request.form.get("vin") or ""
        try:
            decoder = NhtsaVinDecoder(
                config.nhtsa_api_base, config.nhtsa_timeout_seconds
            )
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
        vehicle = _json_field("vehicle_json", {})
        try:
            mileage = int(_field("mileage"))
            if mileage < 0:
                raise ValueError
        except ValueError:
            return jsonify({"ok": False, "error": "Enter a valid mileage."}), 400

        try:
            vin = _normalize_optional_vin(_field("vin"))
        except VinDecodeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if not vin and not _has_manual_vehicle(vehicle):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Enter a valid VIN or select year, make, and model.",
                    }
                ),
                400,
            )

        if vin:
            vehicle["vin"] = vin
        else:
            vehicle["vin"] = ""
        condition_answers = _json_field("condition_json", {})
        photo_labels = _json_field("photo_labels_json", [])
        files = request.files.getlist("photos")
        sanitized_labels = _labels_for_files(photo_labels, len(files))

        if vin and (not vehicle.get("make") or not vehicle.get("model")):
            try:
                decoded = NhtsaVinDecoder(
                    config.nhtsa_api_base, config.nhtsa_timeout_seconds
                ).decode(vin)
                vehicle.update(
                    {
                        key: value
                        for key, value in decoded.to_dict().items()
                        if value and key != "raw"
                    }
                )
            except Exception:
                pass

        aggregator = MarketDataAggregator(config)
        market = aggregator.fetch_bundle(config.database_path, vehicle, mileage, dealer)
        result = calculate_valuation(
            dealer=dealer,
            vehicle=vehicle,
            mileage=mileage,
            condition_answers=condition_answers,
            photo_labels=sanitized_labels,
            market=market,
        )
        photo_summary = _build_photo_summary(files, sanitized_labels)
        ai_review = request_ai_trade_review(
            dealer=dealer,
            vehicle=vehicle,
            mileage=mileage,
            condition_answers=condition_answers,
            photo_files=files,
            photo_labels=sanitized_labels,
            market=market,
            result=result,
        )
        result = apply_ai_trade_review(result, ai_review, dealer)
        photo_summary = attach_ai_photo_findings(photo_summary, ai_review)
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
        _save_photos(
            config.uploads_path,
            config.database_path,
            result.public_id,
            valuation_id,
            files,
            sanitized_labels,
        )
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

    @bp.get("/api/valuations/<public_id>/trend")
    def valuation_trend(public_id: str) -> Response:
        valuation = fetch_valuation_by_public_id(config.database_path, public_id)
        if valuation is None:
            return jsonify({"ok": False, "error": "Valuation not found."}), 404
        return jsonify({"ok": True, "trend": build_trade_value_trend(valuation)})

    @bp.post("/api/valuations/<public_id>/pricing-reasoning")
    def valuation_pricing_reasoning(public_id: str) -> Response:
        valuation = fetch_valuation_by_public_id(config.database_path, public_id)
        if valuation is None:
            return jsonify({"ok": False, "error": "Valuation not found."}), 404
        dealer = fetch_dealer_by_slug(config.database_path, valuation["dealer_slug"])
        if dealer is None:
            return jsonify({"ok": False, "error": "Dealer not found."}), 404
        reasoning = request_ai_pricing_reasoning(
            dealer=dealer,
            valuation=_valuation_payload(valuation),
        )
        if reasoning.get("error"):
            return (
                jsonify(
                    {"ok": False, "error": reasoning["error"], "reasoning": reasoning}
                ),
                400,
            )
        return jsonify({"ok": True, "reasoning": reasoning})

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
            "notes": _appointment_notes(payload),
            "appointment_type": _appointment_type(payload),
            "confirmation_code": f"AT-{secrets.token_hex(3).upper()}",
        }
        if not all(
            [
                customer["name"],
                customer["email"],
                customer["phone"],
                appointment["scheduled_date"],
                appointment["scheduled_time"],
            ]
        ):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Contact details and appointment time are required.",
                    }
                ),
                400,
            )
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
            {
                "valuation": _valuation_payload(updated),
                "customer": customer,
                "appointment": appointment,
                "deal": _deal_payload(payload),
            },
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
                openai_api_key = request.form.get("openai_api_key", "").strip()
                if openai_api_key:
                    dealer = fetch_dealer_by_slug(
                        config.database_path, config.default_dealer_slug
                    )
                    dealer = dealer or fetch_first_dealer(config.database_path)
                    if dealer is not None:
                        update_dealer(
                            config.database_path,
                            dealer["id"],
                            {"openai_api_key": openai_api_key},
                        )
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
        selected_slug = request.args.get("dealer") or (
            dealers[0]["slug"] if dealers else ""
        )
        dealer = fetch_dealer_by_slug(config.database_path, selected_slug) or (
            dealers[0] if dealers else None
        )
        if dealer is None:
            abort(500)
        stats = fetch_dashboard_stats(config.database_path, dealer["id"])
        leads = list_dealer_leads(config.database_path, dealer["id"])
        incentives = list_incentives(
            config.database_path, dealer["id"], active_only=False
        )
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
            market_region=config.market_region,
            market_imported=request.args.get("market_imported", ""),
        )

    @bp.get("/admin/dashboard")
    @_require_admin
    def admin_visual_dashboard() -> str:
        dealers = list_dealers(config.database_path)
        selected_slug = request.args.get("dealer") or (
            dealers[0]["slug"] if dealers else ""
        )
        dealer = fetch_dealer_by_slug(config.database_path, selected_slug) or (
            dealers[0] if dealers else None
        )
        if dealer is None:
            abort(500)
        insights = fetch_admin_dashboard_metrics(config.database_path, dealer["id"])
        leads = list_dealer_leads(config.database_path, dealer["id"], limit=8)
        data_sources = list_data_source_status(config.database_path)
        return render_template(
            "admin_dashboard.html",
            dealers=dealers,
            dealer=dealer,
            insights=insights,
            leads=leads,
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
            "appointment_timezone": request.form.get(
                "appointment_timezone", "America/New_York"
            ),
            "bonus_credit_enabled": (
                1 if request.form.get("bonus_credit_enabled") else 0
            ),
            "bonus_credit_amount": int(request.form.get("bonus_credit_amount") or 0),
            "valuation_hold_days": int(request.form.get("valuation_hold_days") or 10),
            "max_retail_percent": float(request.form.get("max_retail_percent") or 0.95),
            "crm_webhook_url": request.form.get("crm_webhook_url", ""),
            "openai_model": request.form.get("openai_model", "gpt-4.1-mini"),
            "openai_valuation_enabled": (
                1 if request.form.get("openai_valuation_enabled") else 0
            ),
            "openai_image_analysis_enabled": (
                1 if request.form.get("openai_image_analysis_enabled") else 0
            ),
            "openai_price_adjustment_limit_percent": _float_or_default(
                request.form.get("openai_price_adjustment_limit_percent"), 0.06
            ),
            "openai_pricing_reasoning_preprompt": request.form.get(
                "openai_pricing_reasoning_preprompt", ""
            ),
            "market_source_manheim_enabled": (
                1 if request.form.get("market_source_manheim_enabled") else 0
            ),
            "market_source_jd_power_enabled": (
                1 if request.form.get("market_source_jd_power_enabled") else 0
            ),
            "market_source_black_book_enabled": (
                1 if request.form.get("market_source_black_book_enabled") else 0
            ),
            "market_source_kbb_enabled": (
                1 if request.form.get("market_source_kbb_enabled") else 0
            ),
            "market_source_dealer_import_enabled": (
                1 if request.form.get("market_source_dealer_import_enabled") else 0
            ),
            "market_source_demo_fallback_enabled": (
                1 if request.form.get("market_source_demo_fallback_enabled") else 0
            ),
        }
        if request.form.get("clear_openai_api_key"):
            fields["openai_api_key"] = ""
        elif request.form.get("openai_api_key", "").strip():
            fields["openai_api_key"] = request.form.get("openai_api_key", "").strip()
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

    @bp.post("/admin/market-import")
    @_require_admin
    def admin_market_import() -> Response:
        upload = request.files.get("market_csv")
        dealer_slug = request.form.get("dealer_slug") or config.default_dealer_slug
        if upload is None or not upload.filename:
            return redirect(url_for("autotrade.admin_dashboard", dealer=dealer_slug))

        imports_path = config.data_dir / "imports"
        imports_path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        filename = secure_filename(upload.filename) or "market-import.csv"
        csv_path = imports_path / f"{timestamp}-{filename}"
        upload.save(csv_path)
        imported = import_market_csv(
            config.database_path,
            csv_path,
            request.form.get("region") or config.market_region,
            source_override=request.form.get("source", "").strip(),
            replace_source=bool(request.form.get("replace_source")),
        )
        return redirect(
            url_for(
                "autotrade.admin_dashboard",
                dealer=dealer_slug,
                market_imported=imported,
            )
            + "#feeds"
        )

    @bp.get("/admin/export.csv")
    @_require_admin
    def admin_export_csv() -> Response:
        dealer = fetch_first_dealer(config.database_path)
        if dealer is None:
            abort(404)
        rows = list_dealer_leads(config.database_path, dealer["id"], limit=5000)
        handle = StringIO()
        writer = csv.DictWriter(
            handle, fieldnames=list(rows[0].keys()) if rows else ["public_id"]
        )
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

    # ------------------------------------------------------------------
    # Public inventory API
    # ------------------------------------------------------------------

    @bp.get("/api/dealers/<dealer_slug>/inventory")
    def public_inventory(dealer_slug: str) -> Response:
        dealer = _dealer_or_404(config, dealer_slug)
        limit = min(int(request.args.get("limit", 200)), 5000)
        offset = int(request.args.get("offset", 0))
        vehicles = list_inventory_vehicles(
            config.database_path,
            dealer["id"],
            status="active",
            limit=limit,
            offset=offset,
        )
        total = count_inventory_vehicles(
            config.database_path, dealer["id"], status="active"
        )
        return jsonify(
            {
                "ok": True,
                "vehicles": vehicles,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    # ------------------------------------------------------------------
    # Admin inventory routes
    # ------------------------------------------------------------------

    @bp.get("/admin/inventory")
    @_require_admin
    def admin_inventory() -> str:
        dealers = list_dealers(config.database_path)
        selected_slug = request.args.get("dealer") or (
            dealers[0]["slug"] if dealers else ""
        )
        dealer = fetch_dealer_by_slug(config.database_path, selected_slug) or (
            dealers[0] if dealers else None
        )
        if dealer is None:
            abort(500)
        sources = list_inventory_sources(config.database_path, dealer["id"])
        vehicles = list_inventory_vehicles(
            config.database_path, dealer["id"], status="all"
        )
        total = count_inventory_vehicles(
            config.database_path, dealer["id"], status="all"
        )
        active = count_inventory_vehicles(
            config.database_path, dealer["id"], status="active"
        )
        return render_template(
            "admin_inventory.html",
            dealers=dealers,
            dealer=dealer,
            sources=sources,
            vehicles=vehicles,
            total=total,
            active=active,
            sync_result=request.args.get("sync_result", ""),
            sync_error=request.args.get("sync_error", ""),
        )

    @bp.post("/admin/inventory/sources")
    @_require_admin
    def admin_inventory_add_source() -> Response:
        dealer_id = int(request.form.get("dealer_id", 0))
        url = request.form.get("url", "").strip()
        label = request.form.get("label", "").strip()
        dealer_slug = request.form.get("dealer_slug", config.default_dealer_slug)
        if not url:
            return redirect(url_for("autotrade.admin_inventory", dealer=dealer_slug))
        if not label:
            label = url
        create_inventory_source(config.database_path, dealer_id, url, label)
        return redirect(url_for("autotrade.admin_inventory", dealer=dealer_slug))

    @bp.post("/admin/inventory/sources/<int:source_id>/delete")
    @_require_admin
    def admin_inventory_delete_source(source_id: int) -> Response:
        dealer_slug = request.form.get("dealer_slug", config.default_dealer_slug)
        delete_inventory_source(config.database_path, source_id)
        return redirect(url_for("autotrade.admin_inventory", dealer=dealer_slug))

    @bp.post("/admin/inventory/sources/<int:source_id>/sync")
    @_require_admin
    def admin_inventory_sync_source(source_id: int) -> Response:
        dealer_slug = request.form.get("dealer_slug", config.default_dealer_slug)
        source = get_inventory_source(config.database_path, source_id)
        if source is None:
            return redirect(url_for("autotrade.admin_inventory", dealer=dealer_slug))
        dealer = fetch_dealer_by_slug(
            config.database_path, dealer_slug
        ) or fetch_first_dealer(config.database_path)
        openai_key = dealer.get("openai_api_key", "") if dealer else ""
        openai_model = (
            dealer.get("openai_model", "gpt-4.1-mini") if dealer else "gpt-4.1-mini"
        )
        try:
            result = scrape_inventory(
                source["url"],
                openai_api_key=openai_key,
                openai_model=openai_model,
                max_vehicles=5000,
            )
            vehicles = result.get("vehicles", [])
            errors = result.get("errors", [])
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            count = upsert_inventory_vehicles(
                config.database_path,
                source["dealer_id"],
                source_id,
                vehicles,
                now,
            )
            update_inventory_source_sync_result(
                config.database_path,
                source_id,
                count=count,
                status="ok",
                error="; ".join(errors) if errors else "",
            )
            sync_msg = (
                f"Synced {count} vehicles (strategy: {result.get('source', '?')})"
            )
            if errors:
                sync_msg += f" — warnings: {'; '.join(errors[:2])}"
            return redirect(
                url_for(
                    "autotrade.admin_inventory",
                    dealer=dealer_slug,
                    sync_result=sync_msg,
                )
            )
        except Exception as exc:
            update_inventory_source_sync_result(
                config.database_path,
                source_id,
                count=0,
                status="error",
                error=str(exc)[:500],
            )
            return redirect(
                url_for(
                    "autotrade.admin_inventory",
                    dealer=dealer_slug,
                    sync_error=str(exc)[:200],
                )
            )

    @bp.post("/admin/inventory/sources/<int:source_id>/scrape-preview")
    @_require_admin
    def admin_inventory_scrape_preview(source_id: int) -> Response:
        """Return a JSON preview of what would be scraped (no DB write)."""
        source = get_inventory_source(config.database_path, source_id)
        if source is None:
            return jsonify({"ok": False, "error": "Source not found"}), 404
        dealer_slug = request.form.get("dealer_slug", config.default_dealer_slug)
        dealer = fetch_dealer_by_slug(
            config.database_path, dealer_slug
        ) or fetch_first_dealer(config.database_path)
        openai_key = dealer.get("openai_api_key", "") if dealer else ""
        openai_model = (
            dealer.get("openai_model", "gpt-4.1-mini") if dealer else "gpt-4.1-mini"
        )
        result = scrape_inventory(
            source["url"],
            openai_api_key=openai_key,
            openai_model=openai_model,
            max_vehicles=10,
        )
        return jsonify({"ok": True, **result})

    @bp.post("/admin/inventory/<int:vehicle_id>/edit")
    @_require_admin
    def admin_inventory_edit_vehicle(vehicle_id: int) -> Response:
        dealer_slug = request.form.get("dealer_slug", config.default_dealer_slug)
        images_raw = request.form.get("images", "[]")
        try:
            images = json.loads(images_raw)
            if not isinstance(images, list):
                images = []
        except Exception:
            images = []
        fields = {
            "year": _int_or_none(request.form.get("year")),
            "make": request.form.get("make", "").strip().upper(),
            "model": request.form.get("model", "").strip().upper(),
            "trim": request.form.get("trim", "").strip(),
            "body_style": request.form.get("body_style", "").strip(),
            "price": _int_or_none(request.form.get("price")),
            "mileage": _int_or_none(request.form.get("mileage")),
            "ext_color": request.form.get("ext_color", "").strip(),
            "int_color": request.form.get("int_color", "").strip(),
            "transmission": request.form.get("transmission", "").strip(),
            "drivetrain": request.form.get("drivetrain", "").strip(),
            "engine": request.form.get("engine", "").strip(),
            "description": request.form.get("description", "").strip(),
            "detail_url": request.form.get("detail_url", "").strip(),
            "status": request.form.get("status", "active"),
            "notes": request.form.get("notes", "").strip(),
            "images_json": json.dumps(images),
        }
        update_inventory_vehicle(config.database_path, vehicle_id, fields)
        return redirect(url_for("autotrade.admin_inventory", dealer=dealer_slug))

    @bp.post("/admin/inventory/<int:vehicle_id>/delete")
    @_require_admin
    def admin_inventory_delete_vehicle(vehicle_id: int) -> Response:
        dealer_slug = request.form.get("dealer_slug", config.default_dealer_slug)
        delete_inventory_vehicle(config.database_path, vehicle_id)
        return redirect(url_for("autotrade.admin_inventory", dealer=dealer_slug))

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


def _appointment_type(payload: dict[str, Any]) -> str:
    requested = str(payload.get("appointment_type", "")).strip()
    if requested in {"trade_appraisal", "trade_purchase"}:
        return requested
    return "trade_purchase" if _deal_payload(payload) else "trade_appraisal"


def _appointment_notes(payload: dict[str, Any]) -> str:
    notes = str(payload.get("notes", "")).strip()
    deal = _deal_payload(payload)
    if not deal:
        return notes

    purchase = deal.get("purchase_vehicle", {})
    estimate = deal.get("deal_estimate", {})
    summary: list[str] = []
    if notes:
        summary.append(notes)

    vehicle_name = " ".join(
        str(purchase.get(key, "")).strip()
        for key in ["year", "make", "model", "trim"]
        if str(purchase.get(key, "")).strip()
    )
    if vehicle_name:
        stock = str(purchase.get("stock_number") or "").strip()
        stock_note = f" Stock {stock}." if stock else ""
        summary.append(f"Purchase vehicle: {vehicle_name}.{stock_note}")

    estimate_lines = [
        ("Price", estimate.get("purchase_price")),
        ("Trade credit", estimate.get("trade_offer")),
        ("Estimated tax and fees", estimate.get("taxes_and_fees")),
        ("Estimated balance", estimate.get("net_after_trade")),
        ("Down payment", estimate.get("down_payment")),
        ("Amount financed", estimate.get("amount_financed")),
        ("Estimated monthly payment", estimate.get("monthly_payment")),
    ]
    formatted = [
        f"{label}: ${int(value):,}"
        for label, value in estimate_lines
        if isinstance(value, int)
    ]
    term = estimate.get("term_months")
    apr = estimate.get("apr_percent")
    if isinstance(term, int) and term > 0:
        formatted.append(f"Term: {term} months")
    if isinstance(apr, (int, float)) and apr >= 0:
        formatted.append(f"APR: {apr:.2f}%")
    if formatted:
        summary.append("Deal estimate: " + "; ".join(formatted) + ".")

    return "\n".join(summary)


def _deal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    purchase = payload.get("purchase_vehicle") or payload.get("selected_purchase") or {}
    estimate = payload.get("deal_estimate") or {}
    if not isinstance(purchase, dict):
        purchase = {}
    if not isinstance(estimate, dict):
        estimate = {}

    vehicle = {
        "id": _int_or_none(purchase.get("id")),
        "stock_number": str(purchase.get("stock_number") or "").strip(),
        "vin": str(purchase.get("vin") or "").strip(),
        "year": _int_or_none(purchase.get("year")),
        "make": str(purchase.get("make") or "").strip(),
        "model": str(purchase.get("model") or "").strip(),
        "trim": str(purchase.get("trim") or "").strip(),
        "body_style": str(purchase.get("body_style") or "").strip(),
        "price": _int_or_none(purchase.get("price")),
        "mileage": _int_or_none(purchase.get("mileage")),
        "detail_url": str(purchase.get("detail_url") or "").strip(),
    }
    vehicle = {
        key: value
        for key, value in vehicle.items()
        if value not in (None, "")
    }

    deal_estimate: dict[str, Any] = {}
    integer_fields = [
        "purchase_price",
        "trade_offer",
        "taxes_and_fees",
        "doc_fee",
        "net_after_trade",
        "down_payment",
        "amount_financed",
        "monthly_payment",
        "term_months",
    ]
    for field in integer_fields:
        parsed = _int_or_none(estimate.get(field))
        if parsed is not None:
            deal_estimate[field] = parsed
    apr = _float_or_default(estimate.get("apr_percent"), -1)
    if apr >= 0:
        deal_estimate["apr_percent"] = round(apr, 3)

    if not vehicle and not deal_estimate:
        return {}
    return {"purchase_vehicle": vehicle, "deal_estimate": deal_estimate}


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


def _normalize_optional_vin(vin: str) -> str:
    vin = str(vin or "").strip()
    return normalize_vin(vin) if vin else ""


def _has_manual_vehicle(vehicle: Any) -> bool:
    if not isinstance(vehicle, dict):
        return False
    return all(str(vehicle.get(key, "")).strip() for key in ["year", "make", "model"])


def _int_or_none(value: object) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _float_or_default(value: object, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _labels_for_files(labels: Any, file_count: int) -> list[str]:
    fallback = ["front", "rear", "interior", "dash", "tires"]
    if not isinstance(labels, list):
        labels = []
    normalized = [str(label).strip().lower() for label in labels if str(label).strip()]
    while len(normalized) < file_count:
        normalized.append(
            fallback[len(normalized)] if len(normalized) < len(fallback) else "other"
        )
    return normalized[:file_count]


def _build_photo_summary(
    files: list[FileStorage], labels: list[str]
) -> list[dict[str, Any]]:
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

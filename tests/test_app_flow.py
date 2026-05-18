from __future__ import annotations

from io import BytesIO
import json

import pytest

from autotrade_pro import create_app
from autotrade_pro.config import AppConfig
from autotrade_pro.database import database_backend, fetch_dealer_by_slug, update_dealer


def _app(tmp_path):
    config = AppConfig(data_dir=tmp_path, admin_password="test-pass")
    config.ensure_directories()
    app = create_app(config)
    app.config.update(TESTING=True)
    return app


def _app_with_config(tmp_path, **overrides):
    config = AppConfig(data_dir=tmp_path, admin_password="test-pass", **overrides)
    config.ensure_directories()
    app = create_app(config)
    app.config.update(TESTING=True)
    return app


def test_public_valuation_and_appointment_flow(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    valuation_response = client.post(
        "/api/dealers/south-florida-demo/valuations",
        data={
            "vin": "1HGCM82633A004352",
            "mileage": "42000",
            "vehicle_json": json.dumps(
                {
                    "vin": "1HGCM82633A004352",
                    "year": 2021,
                    "make": "HONDA",
                    "model": "ACCORD",
                    "trim": "EX-L",
                    "body_style": "Sedan",
                }
            ),
            "condition_json": json.dumps(
                {
                    "dents": "none",
                    "interior": "clean",
                    "warning_lights": "none",
                    "tires": "7_18",
                    "brakes": "7_18",
                    "oil_change": "0_3",
                }
            ),
            "photo_labels_json": json.dumps(["front", "rear", "interior", "dash", "tires"]),
            "photos": [
                (BytesIO(b"front"), "front.jpg"),
                (BytesIO(b"rear"), "rear.jpg"),
                (BytesIO(b"interior"), "interior.jpg"),
                (BytesIO(b"dash"), "dash.jpg"),
                (BytesIO(b"tires"), "tires.jpg"),
            ],
        },
        content_type="multipart/form-data",
    )

    assert valuation_response.status_code == 200
    valuation_payload = valuation_response.get_json()
    valuation = valuation_payload["valuation"]
    assert valuation["trade_offer"] <= valuation["cap_value"]
    assert valuation["public_id"].startswith("ATP-")

    trend_response = client.get(f"/api/valuations/{valuation['public_id']}/trend")
    assert trend_response.status_code == 200
    trend = trend_response.get_json()["trend"]
    assert trend["normal_annual_miles"] == 12000
    assert trend["history_years"] == 5
    assert trend["projection_years"] == 5
    assert len(trend["points"]) == 11
    now_point = next(point for point in trend["points"] if point["year_offset"] == 0)
    assert now_point["trade_value"] == valuation["trade_offer"]
    assert now_point["projected"] is False
    assert now_point["historical"] is False
    assert all(point["historical"] for point in trend["points"] if point["year_offset"] < 0)
    assert all(point["projected"] for point in trend["points"] if point["year_offset"] > 0)
    assert trend["points"][0]["year_offset"] == -5
    assert trend["points"][0]["mileage"] == 0
    assert trend["points"][-1]["year_offset"] == 5
    assert trend["points"][-1]["mileage"] == 42000 + 60000
    trend_values = [point["trade_value"] for point in trend["points"]]
    assert trend_values == sorted(trend_values, reverse=True)

    appointment_response = client.post(
        f"/api/valuations/{valuation['public_id']}/appointments",
        json={
            "name": "Jamie Driver",
            "email": "jamie@example.com",
            "phone": "305-555-0199",
            "scheduled_date": "2026-06-01",
            "scheduled_time": "10:00 AM",
            "notes": "Interested in a hybrid SUV.",
            "marketing_consent": True,
        },
    )

    assert appointment_response.status_code == 200
    appointment_payload = appointment_response.get_json()
    assert appointment_payload["valuation"]["appointment_status"] == "booked"
    assert appointment_payload["valuation"]["confirmation_code"].startswith("AT-")


def test_database_backend_uses_postgres_url_when_configured(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@example.com:5432/app")
    assert database_backend() == "postgresql"
    monkeypatch.delenv("DATABASE_URL")
    assert database_backend() == "sqlite"


def test_admin_requires_login_and_renders_dashboard(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    protected = client.get("/admin")
    assert protected.status_code == 302
    assert "/admin/login" in protected.headers["Location"]

    login = client.post("/admin/login", data={"password": "test-pass"}, follow_redirects=True)
    assert login.status_code == 200
    assert b"Lead Dashboard" in login.data
    assert b"White Label Settings" in login.data
    assert b"AI valuation assist" in login.data
    assert b"Pricing reasoning preprompt" in login.data
    assert b"Valuation source data" in login.data
    assert b"Kelley Blue Book" in login.data
    assert b"/admin/dashboard" in login.data


def test_admin_can_save_pricing_reasoning_preprompt(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    dealer = fetch_dealer_by_slug(tmp_path / "autotrade.db", "south-florida-demo")

    client.post("/admin/login", data={"password": "test-pass"})
    response = client.post(
        f"/admin/dealer/{dealer['id']}",
        data={
            "name": dealer["name"],
            "legal_name": dealer["legal_name"],
            "logo_url": dealer["logo_url"],
            "hero_image_url": dealer["hero_image_url"],
            "primary_color": dealer["primary_color"],
            "accent_color": dealer["accent_color"],
            "phone": dealer["phone"],
            "email": dealer["email"],
            "address_line1": dealer["address_line1"],
            "city": dealer["city"],
            "state": dealer["state"],
            "postal_code": dealer["postal_code"],
            "appointment_timezone": dealer["appointment_timezone"],
            "bonus_credit_enabled": "on",
            "bonus_credit_amount": str(dealer["bonus_credit_amount"]),
            "valuation_hold_days": str(dealer["valuation_hold_days"]),
            "max_retail_percent": str(dealer["max_retail_percent"]),
            "crm_webhook_url": dealer["crm_webhook_url"],
            "openai_model": dealer["openai_model"],
            "openai_price_adjustment_limit_percent": str(dealer["openai_price_adjustment_limit_percent"]),
            "openai_image_analysis_enabled": "on",
            "openai_pricing_reasoning_preprompt": "Use a warm but direct sales manager voice.",
            "market_source_kbb_enabled": "on",
            "market_source_manheim_enabled": "on",
            "market_source_jd_power_enabled": "on",
            "market_source_black_book_enabled": "on",
            "market_source_dealer_import_enabled": "on",
            "market_source_demo_fallback_enabled": "on",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    updated = fetch_dealer_by_slug(tmp_path / "autotrade.db", "south-florida-demo")
    assert updated["openai_pricing_reasoning_preprompt"] == "Use a warm but direct sales manager voice."


def test_pricing_reasoning_endpoint_uses_openai_and_admin_preprompt(tmp_path, monkeypatch):
    app = _app(tmp_path)
    client = app.test_client()
    dealer = fetch_dealer_by_slug(tmp_path / "autotrade.db", "south-florida-demo")
    update_dealer(
        tmp_path / "autotrade.db",
        dealer["id"],
        {
            "openai_api_key": "sk-test",
            "openai_model": "gpt-test",
            "openai_pricing_reasoning_preprompt": "Use service-lane language.",
        },
    )
    valuation_response = client.post(
        "/api/dealers/south-florida-demo/valuations",
        data={
            "vin": "",
            "mileage": "51000",
            "vehicle_json": json.dumps(
                {
                    "vin": "",
                    "year": 2022,
                    "make": "TOYOTA",
                    "model": "RAV4",
                    "trim": "XLE",
                    "body_style": "SUV",
                }
            ),
            "condition_json": json.dumps(
                {
                    "dents": "none",
                    "interior": "clean",
                    "warning_lights": "none",
                    "tires": "7_18",
                    "brakes": "7_18",
                    "oil_change": "3_6",
                }
            ),
            "photo_labels_json": json.dumps([]),
        },
    )
    public_id = valuation_response.get_json()["valuation"]["public_id"]
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "output_text": json.dumps(
                    {
                        "headline": "Market-backed trade estimate",
                        "explanation": "The offer reflects mileage, condition, and regional trade data.",
                        "value_drivers": [
                            {
                                "label": "Mileage",
                                "impact": "negative",
                                "detail": "Mileage is above the expected baseline.",
                            }
                        ],
                        "confidence_note": "Confidence is based on available source data.",
                        "disclaimer": "Final appraisal may change after inspection.",
                    }
                )
            }

    def fake_post(*args, **kwargs):
        captured["json"] = kwargs["json"]
        return FakeResponse()

    monkeypatch.setattr("autotrade_pro.ai_valuation.requests.post", fake_post)
    response = client.post(f"/api/valuations/{public_id}/pricing-reasoning")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["reasoning"]["headline"] == "Market-backed trade estimate"
    assert payload["reasoning"]["model"] == "gpt-test"
    request_payload = captured["json"]
    assert request_payload["model"] == "gpt-test"
    assert "Use service-lane language." in request_payload["input"][0]["content"]


def test_admin_visual_dashboard_renders_charts_and_metrics(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    protected = client.get("/admin/dashboard")
    assert protected.status_code == 302
    assert "/admin/login" in protected.headers["Location"]

    valuation_response = client.post(
        "/api/dealers/south-florida-demo/valuations",
        data={
            "vin": "",
            "mileage": "51000",
            "vehicle_json": json.dumps(
                {
                    "vin": "",
                    "year": 2022,
                    "make": "TOYOTA",
                    "model": "TACOMA",
                    "trim": "TRD Off-Road",
                    "body_style": "Pickup",
                }
            ),
            "condition_json": json.dumps(
                {
                    "dents": "small",
                    "interior": "clean",
                    "warning_lights": "none",
                    "tires": "7_18",
                    "brakes": "7_18",
                    "oil_change": "3_6",
                }
            ),
            "photo_labels_json": json.dumps(["front", "rear", "interior"]),
        },
    )
    assert valuation_response.status_code == 200
    public_id = valuation_response.get_json()["valuation"]["public_id"]

    appointment_response = client.post(
        f"/api/valuations/{public_id}/appointments",
        json={
            "name": "Taylor Buyer",
            "email": "taylor@example.com",
            "phone": "305-555-0142",
            "scheduled_date": "2026-06-02",
            "scheduled_time": "2:30 PM",
            "notes": "",
            "marketing_consent": False,
        },
    )
    assert appointment_response.status_code == 200

    client.post("/admin/login", data={"password": "test-pass"})
    dashboard = client.get("/admin/dashboard")

    assert dashboard.status_code == 200
    assert b"Performance Dashboard" in dashboard.data
    assert b"Valuation Trend" in dashboard.data
    assert b"Booking Conversion" in dashboard.data
    assert b"Offer Bands" in dashboard.data
    assert b"Market Mix" in dashboard.data
    assert b'id="valuationTrend"' in dashboard.data
    assert b"Taylor Buyer" in dashboard.data
    assert b"100% booking rate" in dashboard.data


def test_admin_can_bulk_import_market_csv(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    client.post("/admin/login", data={"password": "test-pass"})

    csv_body = "\n".join(
        [
            "year,make,model,trim,region,retail_value,trade_in_value,confidence",
            "2016,HONDA,CR-V,Base,south_florida,12600,9200,0.93",
            "2017,TOYOTA,CAMRY,SE,south_florida,13500,9800,0.91",
        ]
    )
    response = client.post(
        "/admin/market-import",
        data={
            "dealer_slug": "south-florida-demo",
            "source": "kelley_blue_book_production",
            "region": "south_florida",
            "replace_source": "on",
            "market_csv": (BytesIO(csv_body.encode("utf-8")), "kbb.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Market import complete" in response.data
    assert b"2 rows imported" in response.data
    assert b"kelley_blue_book_production" in response.data

    valuation_response = client.post(
        "/api/dealers/south-florida-demo/valuations",
        data={
            "vin": "",
            "mileage": "150000",
            "vehicle_json": json.dumps(
                {
                    "vin": "",
                    "year": 2016,
                    "make": "HONDA",
                    "model": "CR-V",
                    "trim": "Base",
                    "body_style": "SUV",
                }
            ),
            "condition_json": json.dumps(
                {
                    "dents": "none",
                    "interior": "clean",
                    "warning_lights": "none",
                    "tires": "0_6",
                    "brakes": "0_6",
                    "oil_change": "0_3",
                }
            ),
            "photo_labels_json": json.dumps(["front", "rear", "interior", "dash", "tires"]),
        },
    )

    assert valuation_response.status_code == 200
    valuation = valuation_response.get_json()["valuation"]
    signal = valuation["source_breakdown"]["signals"][0]
    assert signal["source"] == "kelley_blue_book_production"
    assert signal["retail_value"] == 12600
    assert signal["wholesale_value"] == 9200


def test_public_vehicle_screen_has_dropdown_selectors(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    response = client.get("/d/south-florida-demo")

    assert response.status_code == 200
    assert b'<select id="year">' in response.data
    assert b'<select id="make">' in response.data
    assert b'<select id="model">' in response.data
    assert b'<select id="trim">' in response.data
    assert b'<select id="bodyStyle">' in response.data
    assert b'id="makeSearch"' in response.data
    assert b'id="modelSearch"' in response.data
    assert b'id="trimSearch"' in response.data
    assert b'id="bodyStyleSearch"' in response.data
    assert b'data-search-select="make"' in response.data
    assert b'id="reasoningBtn"' in response.data
    assert b'id="reasoningModal"' in response.data
    assert b"Refreshing offer record before booking" in response.data
    assert b"isMissingValuationError" in response.data


def test_manual_dropdown_vehicle_can_be_valued_without_vin(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/api/dealers/south-florida-demo/valuations",
        data={
            "vin": "",
            "mileage": "12750",
            "vehicle_json": json.dumps(
                {
                    "vin": "",
                    "year": 2025,
                    "make": "TOYOTA",
                    "model": "RAV4",
                    "trim": "XLE",
                    "body_style": "SUV",
                }
            ),
            "condition_json": json.dumps(
                {
                    "dents": "none",
                    "interior": "clean",
                    "warning_lights": "none",
                    "tires": "0_6",
                    "brakes": "0_6",
                    "oil_change": "0_3",
                }
            ),
            "photo_labels_json": json.dumps([]),
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["valuation"]["vehicle"]["vin"] == ""
    assert payload["valuation"]["vehicle"]["make"] == "TOYOTA"
    assert payload["valuation"]["vehicle"]["model"] == "RAV4"


def test_vehicle_option_endpoints_have_fallbacks_without_live_nhtsa(tmp_path):
    app = _app_with_config(
        tmp_path,
        nhtsa_api_base="http://127.0.0.1:9/api",
        nhtsa_timeout_seconds=0.01,
    )
    client = app.test_client()

    options = client.get("/api/vehicle-options").get_json()
    models = client.get("/api/vehicle-options/models?make=HONDA&year=2024").get_json()
    trims = client.get("/api/vehicle-options/trims?make=FORD&model=F-150").get_json()

    assert options["ok"] is True
    assert options["source"] == "fallback"
    assert options["makes"][:4] == ["TOYOTA", "HONDA", "FORD", "CHEVROLET"]
    assert "HONDA" in options["makes"]
    assert "SUV" in options["body_styles"]
    assert models["ok"] is True
    assert models["models"][:3] == ["CR-V", "Civic", "Accord"]
    assert "Accord" in models["models"]
    assert trims["ok"] is True
    assert "Lariat" in trims["trims"]


def test_kbb_source_can_be_used_and_toggled_for_estimates(tmp_path, monkeypatch):
    app = _app_with_config(
        tmp_path,
        kbb_api_base="https://kbb.example.test",
        kbb_api_key="kbb-key",
    )
    dealer = fetch_dealer_by_slug(tmp_path / "autotrade.db", "south-florida-demo")
    update_dealer(
        tmp_path / "autotrade.db",
        dealer["id"],
        {
            "market_source_manheim_enabled": 0,
            "market_source_jd_power_enabled": 0,
            "market_source_black_book_enabled": 0,
            "market_source_kbb_enabled": 1,
            "market_source_dealer_import_enabled": 0,
            "market_source_demo_fallback_enabled": 1,
        },
    )

    class FakeKbbResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "trade_in_value": 17800,
                "typical_listing_value": 22600,
                "confidence": 0.91,
                "valuation_id": "fake-kbb-value",
            }

    calls = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeKbbResponse()

    monkeypatch.setattr("autotrade_pro.market_data.requests.get", fake_get)
    client = app.test_client()

    response = client.post(
        "/api/dealers/south-florida-demo/valuations",
        data={
            "vin": "",
            "mileage": "64250",
            "vehicle_json": json.dumps(
                {
                    "vin": "",
                    "year": 2020,
                    "make": "KBBTEST",
                    "model": "SOURCECHECK",
                    "trim": "Touring",
                    "body_style": "Sedan",
                }
            ),
            "condition_json": json.dumps(
                {
                    "dents": "none",
                    "interior": "clean",
                    "warning_lights": "none",
                    "tires": "7_18",
                    "brakes": "7_18",
                    "oil_change": "3_6",
                }
            ),
            "photo_labels_json": json.dumps(["front", "rear", "interior", "dash"]),
        },
    )

    assert response.status_code == 200
    valuation = response.get_json()["valuation"]
    signal = valuation["source_breakdown"]["signals"][0]
    assert calls[0]["url"] == "https://kbb.example.test/valuation"
    assert calls[0]["headers"]["Authorization"] == "Bearer kbb-key"
    assert calls[0]["params"]["make"] == "KBBTEST"
    assert signal["source"] == "kelley_blue_book"
    assert signal["retail_value"] == 22600
    assert signal["wholesale_value"] == 17800
    assert valuation["retail_market_value"] == 22600
    assert valuation["auction_wholesale_value"] == 17800

    update_dealer(
        tmp_path / "autotrade.db",
        dealer["id"],
        {"market_source_kbb_enabled": 0, "market_source_dealer_import_enabled": 0},
    )
    calls.clear()

    disabled_response = client.post(
        "/api/dealers/south-florida-demo/valuations",
        data={
            "vin": "",
            "mileage": "64250",
            "vehicle_json": json.dumps(
                {
                    "vin": "",
                    "year": 2020,
                    "make": "KBBTEST",
                    "model": "SOURCECHECK",
                    "trim": "Touring",
                    "body_style": "Sedan",
                }
            ),
            "condition_json": json.dumps(
                {
                    "dents": "none",
                    "interior": "clean",
                    "warning_lights": "none",
                    "tires": "7_18",
                    "brakes": "7_18",
                    "oil_change": "3_6",
                }
            ),
            "photo_labels_json": json.dumps(["front", "rear", "interior", "dash"]),
        },
    )

    assert disabled_response.status_code == 200
    disabled = disabled_response.get_json()["valuation"]
    assert calls == []
    assert disabled["source_breakdown"]["signals"][0]["source"] == "deterministic_demo_estimate"
    assert "Kelley Blue Book" in " ".join(disabled["source_breakdown"]["notes"])


@pytest.mark.parametrize(
    (
        "year",
        "make",
        "model",
        "trim",
        "body_style",
        "mileage",
        "max_offer",
        "max_retail",
    ),
    [
        (2014, "TOYOTA", "CAMRY", "SE", "Sedan", 160000, 9000, 12000),
        (2016, "TOYOTA", "RAV4", "XLE", "SUV", 150000, 12500, 15500),
        (2015, "HONDA", "ACCORD", "EX-L", "Sedan", 155000, 13000, 16500),
        (2016, "HONDA", "CR-V", "Base", "SUV", 150000, 12000, 15000),
        (2014, "FORD", "F-150", "XLT", "Pickup", 170000, 19000, 24000),
        (2015, "CHEVROLET", "SILVERADO", "LT", "Pickup", 165000, 14000, 18000),
    ],
)
def test_older_high_mileage_snapshot_values_are_normalized(
    tmp_path,
    year,
    make,
    model,
    trim,
    body_style,
    mileage,
    max_offer,
    max_retail,
):
    app = _app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/api/dealers/south-florida-demo/valuations",
        data={
            "vin": "",
            "mileage": str(mileage),
            "vehicle_json": json.dumps(
                {
                    "vin": "",
                    "year": year,
                    "make": make,
                    "model": model,
                    "trim": trim,
                    "body_style": body_style,
                }
            ),
            "condition_json": json.dumps(
                {
                    "dents": "none",
                    "interior": "clean",
                    "warning_lights": "none",
                    "tires": "0_6",
                    "brakes": "0_6",
                    "oil_change": "0_3",
                }
            ),
            "photo_labels_json": json.dumps(["front", "rear", "interior", "dash", "tires"]),
            "photos": [
                (BytesIO(b"front"), "front.jpg"),
                (BytesIO(b"rear"), "rear.jpg"),
                (BytesIO(b"interior"), "interior.jpg"),
                (BytesIO(b"dash"), "dash.jpg"),
                (BytesIO(b"tires"), "tires.jpg"),
            ],
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    valuation = response.get_json()["valuation"]
    signals = valuation["source_breakdown"]["signals"]

    assert valuation["trade_offer"] <= max_offer
    assert valuation["retail_market_value"] <= max_retail
    assert {signal["raw"]["normalization"]["target_year"] for signal in signals} == {year}
    assert all(
        signal["raw"]["normalization"]["source_year"] > year
        for signal in signals
    )


def test_ai_review_can_adjust_price_and_digest_photos(tmp_path, monkeypatch):
    app = _app(tmp_path)
    dealer = fetch_dealer_by_slug(tmp_path / "autotrade.db", "south-florida-demo")
    update_dealer(
        tmp_path / "autotrade.db",
        dealer["id"],
        {
            "openai_api_key": "sk-test",
            "openai_model": "gpt-4.1-mini",
            "openai_valuation_enabled": 1,
            "openai_image_analysis_enabled": 1,
            "openai_price_adjustment_limit_percent": 0.08,
        },
    )

    class FakeOpenAIResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "output_text": json.dumps(
                    {
                        "suggested_trade_offer": 20500,
                        "suggested_low": 19800,
                        "suggested_high": 21100,
                        "confidence": 0.82,
                        "apply_adjustment": True,
                        "price_rationale": "Visible tire wear supports a slightly lower offer.",
                        "value_consistency_notes": "Market data and mileage are broadly consistent.",
                        "risk_flags": ["Confirm tire tread depth in person."],
                        "image_findings": [
                            {
                                "label": "front",
                                "observations": "Front view shows normal wear.",
                                "damage_detected": False,
                                "severity": "minor",
                                "estimated_reconditioning_impact": 150,
                            }
                        ],
                    }
                )
            }

    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"])
        return FakeOpenAIResponse()

    monkeypatch.setattr("autotrade_pro.ai_valuation.requests.post", fake_post)
    client = app.test_client()

    response = client.post(
        "/api/dealers/south-florida-demo/valuations",
        data={
            "vin": "",
            "mileage": "35000",
            "vehicle_json": json.dumps(
                {
                    "vin": "",
                    "year": 2024,
                    "make": "FORD",
                    "model": "F-150",
                    "trim": "Lariat",
                    "body_style": "Pickup",
                }
            ),
            "condition_json": json.dumps(
                {
                    "dents": "small",
                    "interior": "clean",
                    "warning_lights": "none",
                    "tires": "19_36",
                    "brakes": "7_18",
                    "oil_change": "3_6",
                }
            ),
            "photo_labels_json": json.dumps(["front"]),
            "photos": [(BytesIO(b"fake image bytes"), "front.jpg")],
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    valuation = response.get_json()["valuation"]
    assert calls
    assert any(part["type"] == "input_image" for part in calls[0]["input"][1]["content"])
    assert valuation["adjustments"]["ai_review"]["applied"] is True
    assert valuation["adjustments"]["ai_review"]["suggested_trade_offer"] == 20500
    assert valuation["trade_offer"] == valuation["adjustments"]["ai_review"]["adjusted_trade_offer"]
    assert abs(
        valuation["trade_offer"] - valuation["adjustments"]["ai_review"]["pre_ai_trade_offer"]
    ) <= valuation["adjustments"]["ai_review"]["pre_ai_trade_offer"] * 0.08 + 50
    assert valuation["trade_offer"] <= valuation["cap_value"]

    status = client.get(f"/api/valuations/{valuation['public_id']}").get_json()
    assert status["valuation"]["photo_summary"][0]["ai_findings"]["severity"] == "minor"


def test_healthcheck_returns_ok(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}

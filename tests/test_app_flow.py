from __future__ import annotations

from io import BytesIO
import json

from autotrade_pro import create_app
from autotrade_pro.config import AppConfig


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
    assert "HONDA" in options["makes"]
    assert "SUV" in options["body_styles"]
    assert models["ok"] is True
    assert "Accord" in models["models"]
    assert trims["ok"] is True
    assert "Lariat" in trims["trims"]


def test_healthcheck_returns_ok(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}

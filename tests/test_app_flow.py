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

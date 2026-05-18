"""SQLite persistence helpers for AutoTrade Pro."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS dealers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                legal_name TEXT NOT NULL DEFAULT '',
                logo_url TEXT NOT NULL DEFAULT '',
                hero_image_url TEXT NOT NULL DEFAULT '',
                primary_color TEXT NOT NULL DEFAULT '#184E77',
                accent_color TEXT NOT NULL DEFAULT '#F9A03F',
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                address_line1 TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT '',
                postal_code TEXT NOT NULL DEFAULT '',
                appointment_timezone TEXT NOT NULL DEFAULT 'America/New_York',
                bonus_credit_enabled INTEGER NOT NULL DEFAULT 1,
                bonus_credit_amount INTEGER NOT NULL DEFAULT 500,
                valuation_hold_days INTEGER NOT NULL DEFAULT 10,
                max_retail_percent REAL NOT NULL DEFAULT 0.95,
                crm_webhook_url TEXT NOT NULL DEFAULT '',
                openai_api_key TEXT NOT NULL DEFAULT '',
                openai_model TEXT NOT NULL DEFAULT 'gpt-4.1-mini',
                openai_valuation_enabled INTEGER NOT NULL DEFAULT 0,
                openai_image_analysis_enabled INTEGER NOT NULL DEFAULT 1,
                openai_price_adjustment_limit_percent REAL NOT NULL DEFAULT 0.06,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS incentives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dealer_id INTEGER NOT NULL,
                reveal_step TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                value_label TEXT NOT NULL DEFAULT '',
                icon TEXT NOT NULL DEFAULT 'gift',
                active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (dealer_id) REFERENCES dealers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS valuations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL UNIQUE,
                dealer_id INTEGER NOT NULL,
                vin TEXT NOT NULL,
                year INTEGER,
                make TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                trim TEXT NOT NULL DEFAULT '',
                body_style TEXT NOT NULL DEFAULT '',
                mileage INTEGER NOT NULL,
                condition_answers_json TEXT NOT NULL,
                photo_summary_json TEXT NOT NULL,
                vehicle_payload_json TEXT NOT NULL,
                source_data_json TEXT NOT NULL,
                adjustments_json TEXT NOT NULL,
                source_breakdown_json TEXT NOT NULL,
                condition_score REAL NOT NULL,
                condition_grade TEXT NOT NULL,
                retail_market_value INTEGER NOT NULL,
                auction_wholesale_value INTEGER NOT NULL,
                comparable_value INTEGER NOT NULL,
                cap_value INTEGER NOT NULL,
                trade_offer INTEGER NOT NULL,
                valuation_low INTEGER NOT NULL,
                valuation_high INTEGER NOT NULL,
                data_quality_score REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'offer_ready',
                offer_expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (dealer_id) REFERENCES dealers(id)
            );

            CREATE TABLE IF NOT EXISTS vehicle_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                valuation_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                storage_name TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (valuation_id) REFERENCES valuations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                valuation_id INTEGER NOT NULL UNIQUE,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                marketing_consent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (valuation_id) REFERENCES valuations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                valuation_id INTEGER NOT NULL UNIQUE,
                dealer_id INTEGER NOT NULL,
                scheduled_date TEXT NOT NULL,
                scheduled_time TEXT NOT NULL,
                appointment_type TEXT NOT NULL DEFAULT 'trade_appraisal',
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'booked',
                confirmation_code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (valuation_id) REFERENCES valuations(id) ON DELETE CASCADE,
                FOREIGN KEY (dealer_id) REFERENCES dealers(id)
            );

            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vin_pattern TEXT NOT NULL DEFAULT '',
                year INTEGER,
                make TEXT NOT NULL,
                model TEXT NOT NULL,
                trim TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT 'south_florida',
                source TEXT NOT NULL,
                retail_value INTEGER NOT NULL,
                wholesale_value INTEGER NOT NULL,
                sample_size INTEGER NOT NULL DEFAULT 0,
                days_supply INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0.7,
                captured_at TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS crm_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dealer_id INTEGER NOT NULL,
                valuation_id INTEGER,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                response_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (dealer_id) REFERENCES dealers(id),
                FOREIGN KEY (valuation_id) REFERENCES valuations(id)
            );

            CREATE TABLE IF NOT EXISTS valuation_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                valuation_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (valuation_id) REFERENCES valuations(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_valuations_dealer_created
                ON valuations (dealer_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_market_snapshot_lookup
                ON market_snapshots (region, make, model, year);
            """
        )
        _ensure_columns(
            conn,
            "dealers",
            {
                "openai_api_key": "TEXT NOT NULL DEFAULT ''",
                "openai_model": "TEXT NOT NULL DEFAULT 'gpt-4.1-mini'",
                "openai_valuation_enabled": "INTEGER NOT NULL DEFAULT 0",
                "openai_image_analysis_enabled": "INTEGER NOT NULL DEFAULT 1",
                "openai_price_adjustment_limit_percent": "REAL NOT NULL DEFAULT 0.06",
            },
        )
        conn.commit()


def seed_demo_data(db_path: Path, default_slug: str = "south-florida-demo") -> None:
    now = utc_now()
    with connect(db_path) as conn:
        dealer = conn.execute(
            "SELECT id FROM dealers WHERE slug = ?", (default_slug,)
        ).fetchone()
        if dealer is None:
            cursor = conn.execute(
                """
                INSERT INTO dealers (
                    slug, name, legal_name, logo_url, hero_image_url,
                    primary_color, accent_color, phone, email, address_line1,
                    city, state, postal_code, appointment_timezone,
                    bonus_credit_enabled, bonus_credit_amount, valuation_hold_days,
                    max_retail_percent, crm_webhook_url, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    default_slug,
                    "AutoTrade Pro South Florida",
                    "AutoTrade Pro Demo Dealer Group",
                    "",
                    "https://images.unsplash.com/photo-1503376780353-7e6692767b70?auto=format&fit=crop&w=1800&q=80",
                    "#184E77",
                    "#F9A03F",
                    "(305) 555-0188",
                    "trades@exampledealer.com",
                    "1200 Biscayne Blvd",
                    "Miami",
                    "FL",
                    "33132",
                    "America/New_York",
                    1,
                    500,
                    10,
                    0.95,
                    "",
                    now,
                    now,
                ),
            )
            dealer_id = cursor.lastrowid
        else:
            dealer_id = dealer["id"]

        incentive_count = conn.execute(
            "SELECT COUNT(*) AS count FROM incentives WHERE dealer_id = ?",
            (dealer_id,),
        ).fetchone()["count"]
        if incentive_count == 0:
            incentives = [
                (
                    "vin",
                    "Free visit wash",
                    "A complimentary exterior wash is unlocked when you complete your trade profile.",
                    "$35 value",
                    "sparkle",
                    10,
                ),
                (
                    "condition",
                    "Maintenance package",
                    "Eligible trade-in customers can unlock routine maintenance on their next purchase.",
                    "Up to $399",
                    "wrench",
                    20,
                ),
                (
                    "photos",
                    "Priority appraisal lane",
                    "Photo-complete trades move to priority review when you arrive.",
                    "Faster visit",
                    "clock",
                    30,
                ),
                (
                    "offer",
                    "In-app booking bonus",
                    "Book your showroom appointment here and add bonus trade credit at check-in.",
                    "+$500",
                    "calendar",
                    40,
                ),
            ]
            conn.executemany(
                """
                INSERT INTO incentives (
                    dealer_id, reveal_step, title, description, value_label,
                    icon, active, sort_order, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                [
                    (dealer_id, step, title, description, value, icon, order, now, now)
                    for step, title, description, value, icon, order in incentives
                ],
            )

        market_count = conn.execute(
            "SELECT COUNT(*) AS count FROM market_snapshots"
        ).fetchone()["count"]
        if market_count == 0:
            rows = [
                (2023, "TOYOTA", "CAMRY", "SE", "manheim_mmr_demo", 27700, 22200, 214, 42, 0.86),
                (2022, "TOYOTA", "RAV4", "XLE", "manheim_mmr_demo", 30400, 24500, 189, 38, 0.84),
                (2021, "HONDA", "ACCORD", "EX-L", "regional_auction_demo", 25200, 20100, 96, 51, 0.78),
                (2022, "HONDA", "CR-V", "EX", "manheim_mmr_demo", 29600, 23800, 166, 44, 0.82),
                (2021, "FORD", "F-150", "XLT", "regional_auction_demo", 37200, 30900, 144, 48, 0.81),
                (2023, "CHEVROLET", "SILVERADO", "LT", "manheim_mmr_demo", 41400, 34600, 112, 53, 0.79),
                (2020, "BMW", "3 SERIES", "330I", "retail_comparable_demo", 28600, 22200, 88, 59, 0.74),
                (2021, "MERCEDES-BENZ", "C-CLASS", "C300", "retail_comparable_demo", 31800, 24900, 72, 61, 0.73),
                (2022, "NISSAN", "ALTIMA", "SV", "regional_auction_demo", 22300, 17600, 139, 57, 0.77),
                (2021, "HYUNDAI", "SONATA", "SEL", "regional_auction_demo", 21400, 16800, 121, 49, 0.76),
            ]
            conn.executemany(
                """
                INSERT INTO market_snapshots (
                    year, make, model, trim, region, source, retail_value,
                    wholesale_value, sample_size, days_supply, confidence,
                    captured_at, raw_json
                )
                VALUES (?, ?, ?, ?, 'south_florida', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        year,
                        make,
                        model,
                        trim,
                        source,
                        retail,
                        wholesale,
                        sample,
                        days,
                        confidence,
                        now,
                        _json({"seed": True, "region": "south_florida"}),
                    )
                    for year, make, model, trim, source, retail, wholesale, sample, days, confidence in rows
                ],
            )
        conn.commit()


def fetch_dealer_by_slug(db_path: Path, slug: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM dealers WHERE slug = ?", (slug,)).fetchone()
        return _row_to_dict(row)


def fetch_first_dealer(db_path: Path) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM dealers ORDER BY id LIMIT 1").fetchone()
        return _row_to_dict(row)


def list_dealers(db_path: Path) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM dealers ORDER BY name").fetchall()
        return [dict(row) for row in rows]


def update_dealer(db_path: Path, dealer_id: int, fields: dict[str, Any]) -> None:
    allowed = {
        "name",
        "legal_name",
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
        "appointment_timezone",
        "bonus_credit_enabled",
        "bonus_credit_amount",
        "valuation_hold_days",
        "max_retail_percent",
        "crm_webhook_url",
        "openai_api_key",
        "openai_model",
        "openai_valuation_enabled",
        "openai_image_analysis_enabled",
        "openai_price_adjustment_limit_percent",
    }
    assignments = []
    values: list[Any] = []
    for key, value in fields.items():
        if key in allowed:
            assignments.append(f"{key} = ?")
            values.append(value)
    if not assignments:
        return
    assignments.append("updated_at = ?")
    values.extend([utc_now(), dealer_id])
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE dealers SET {', '.join(assignments)} WHERE id = ?",
            tuple(values),
        )
        conn.commit()


def _ensure_columns(
    conn: sqlite3.Connection, table_name: str, columns: dict[str, str]
) -> None:
    existing = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    for column, declaration in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {declaration}")


def list_incentives(db_path: Path, dealer_id: int, active_only: bool = True) -> list[dict[str, Any]]:
    clause = "AND active = 1" if active_only else ""
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM incentives
            WHERE dealer_id = ? {clause}
            ORDER BY sort_order, id
            """,
            (dealer_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def update_incentive(db_path: Path, incentive_id: int, fields: dict[str, Any]) -> None:
    allowed = {
        "reveal_step",
        "title",
        "description",
        "value_label",
        "icon",
        "active",
        "sort_order",
    }
    assignments = []
    values: list[Any] = []
    for key, value in fields.items():
        if key in allowed:
            assignments.append(f"{key} = ?")
            values.append(value)
    if not assignments:
        return
    assignments.append("updated_at = ?")
    values.extend([utc_now(), incentive_id])
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE incentives SET {', '.join(assignments)} WHERE id = ?",
            tuple(values),
        )
        conn.commit()


def fetch_market_snapshots(
    db_path: Path,
    *,
    make: str,
    model: str,
    year: int | None,
    region: str,
) -> list[dict[str, Any]]:
    make_norm = make.strip().upper()
    model_norm = model.strip().upper()
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM market_snapshots
            WHERE region = ?
              AND upper(make) = ?
              AND upper(model) = ?
              AND (year = ? OR ? IS NULL)
            ORDER BY captured_at DESC, confidence DESC
            LIMIT 20
            """,
            (region, make_norm, model_norm, year, year),
        ).fetchall()
        if rows:
            return [dict(row) for row in rows]
        fallback = conn.execute(
            """
            SELECT * FROM market_snapshots
            WHERE region = ? AND upper(make) = ?
            ORDER BY abs(coalesce(year, ?) - ?), confidence DESC
            LIMIT 12
            """,
            (region, make_norm, year or 2024, year or 2024),
        ).fetchall()
        return [dict(row) for row in fallback]


def upsert_market_snapshot(db_path: Path, snapshot: dict[str, Any]) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO market_snapshots (
                vin_pattern, year, make, model, trim, region, source,
                retail_value, wholesale_value, sample_size, days_supply,
                confidence, captured_at, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.get("vin_pattern", ""),
                snapshot.get("year"),
                snapshot["make"].strip().upper(),
                snapshot["model"].strip().upper(),
                snapshot.get("trim", ""),
                snapshot.get("region", "south_florida"),
                snapshot["source"],
                int(snapshot["retail_value"]),
                int(snapshot["wholesale_value"]),
                int(snapshot.get("sample_size", 0)),
                int(snapshot.get("days_supply", 0)),
                float(snapshot.get("confidence", 0.7)),
                snapshot.get("captured_at", utc_now()),
                _json(snapshot.get("raw", {})),
            ),
        )
        conn.commit()


def create_valuation(db_path: Path, payload: dict[str, Any]) -> int:
    now = utc_now()
    fields = [
        "public_id",
        "dealer_id",
        "vin",
        "year",
        "make",
        "model",
        "trim",
        "body_style",
        "mileage",
        "condition_answers_json",
        "photo_summary_json",
        "vehicle_payload_json",
        "source_data_json",
        "adjustments_json",
        "source_breakdown_json",
        "condition_score",
        "condition_grade",
        "retail_market_value",
        "auction_wholesale_value",
        "comparable_value",
        "cap_value",
        "trade_offer",
        "valuation_low",
        "valuation_high",
        "data_quality_score",
        "offer_expires_at",
        "created_at",
        "updated_at",
    ]
    values = [payload.get(field) for field in fields]
    values[-2] = now
    values[-1] = now
    with connect(db_path) as conn:
        cursor = conn.execute(
            f"""
            INSERT INTO valuations ({', '.join(fields)})
            VALUES ({', '.join(['?'] * len(fields))})
            """,
            tuple(values),
        )
        conn.execute(
            """
            INSERT INTO valuation_audit_events (
                valuation_id, event_type, payload_json, created_at
            )
            VALUES (?, 'valuation_created', ?, ?)
            """,
            (cursor.lastrowid, _json({"public_id": payload["public_id"]}), now),
        )
        conn.commit()
        return int(cursor.lastrowid)


def add_vehicle_photo(db_path: Path, valuation_id: int, photo: dict[str, Any]) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO vehicle_photos (
                valuation_id, label, original_filename, storage_name,
                content_type, size_bytes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                valuation_id,
                photo["label"],
                photo["original_filename"],
                photo["storage_name"],
                photo["content_type"],
                photo["size_bytes"],
                utc_now(),
            ),
        )
        conn.commit()


def fetch_valuation_by_public_id(db_path: Path, public_id: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT v.*, d.slug AS dealer_slug, d.name AS dealer_name,
                   d.phone AS dealer_phone, d.email AS dealer_email,
                   d.address_line1, d.city, d.state, d.postal_code,
                   c.name AS customer_name, c.email AS customer_email,
                   c.phone AS customer_phone,
                   a.scheduled_date, a.scheduled_time,
                   a.confirmation_code, a.status AS appointment_status
            FROM valuations v
            JOIN dealers d ON d.id = v.dealer_id
            LEFT JOIN customers c ON c.valuation_id = v.id
            LEFT JOIN appointments a ON a.valuation_id = v.id
            WHERE v.public_id = ?
            """,
            (public_id,),
        ).fetchone()
        valuation = _row_to_dict(row)
        if valuation is None:
            return None
        valuation["photos"] = [
            dict(photo)
            for photo in conn.execute(
                "SELECT * FROM vehicle_photos WHERE valuation_id = ? ORDER BY id",
                (valuation["id"],),
            ).fetchall()
        ]
        return valuation


def create_customer_and_appointment(
    db_path: Path,
    *,
    valuation_id: int,
    dealer_id: int,
    customer: dict[str, Any],
    appointment: dict[str, Any],
) -> None:
    now = utc_now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO customers (
                valuation_id, name, email, phone, marketing_consent, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(valuation_id) DO UPDATE SET
                name = excluded.name,
                email = excluded.email,
                phone = excluded.phone,
                marketing_consent = excluded.marketing_consent
            """,
            (
                valuation_id,
                customer["name"],
                customer["email"],
                customer["phone"],
                int(bool(customer.get("marketing_consent"))),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO appointments (
                valuation_id, dealer_id, scheduled_date, scheduled_time,
                appointment_type, notes, status, confirmation_code,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'booked', ?, ?, ?)
            ON CONFLICT(valuation_id) DO UPDATE SET
                scheduled_date = excluded.scheduled_date,
                scheduled_time = excluded.scheduled_time,
                notes = excluded.notes,
                status = 'booked',
                updated_at = excluded.updated_at
            """,
            (
                valuation_id,
                dealer_id,
                appointment["scheduled_date"],
                appointment["scheduled_time"],
                appointment.get("appointment_type", "trade_appraisal"),
                appointment.get("notes", ""),
                appointment["confirmation_code"],
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE valuations SET status = 'appointment_booked', updated_at = ? WHERE id = ?",
            (now, valuation_id),
        )
        conn.execute(
            """
            INSERT INTO valuation_audit_events (
                valuation_id, event_type, payload_json, created_at
            )
            VALUES (?, 'appointment_booked', ?, ?)
            """,
            (valuation_id, _json(appointment), now),
        )
        conn.commit()


def list_dealer_leads(db_path: Path, dealer_id: int, limit: int = 100) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT v.public_id, v.vin, v.year, v.make, v.model, v.trim,
                   v.mileage, v.trade_offer, v.condition_grade, v.status,
                   v.offer_expires_at, v.created_at,
                   c.name AS customer_name, c.email AS customer_email,
                   c.phone AS customer_phone,
                   a.scheduled_date, a.scheduled_time, a.confirmation_code
            FROM valuations v
            LEFT JOIN customers c ON c.valuation_id = v.id
            LEFT JOIN appointments a ON a.valuation_id = v.id
            WHERE v.dealer_id = ?
            ORDER BY v.created_at DESC
            LIMIT ?
            """,
            (dealer_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def fetch_dashboard_stats(db_path: Path, dealer_id: int) -> dict[str, Any]:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_valuations,
                SUM(CASE WHEN status = 'appointment_booked' THEN 1 ELSE 0 END) AS appointments,
                COALESCE(AVG(trade_offer), 0) AS average_offer,
                COALESCE(AVG(condition_score), 0) AS average_condition
            FROM valuations
            WHERE dealer_id = ?
            """,
            (dealer_id,),
        ).fetchone()
        return dict(row)


def add_crm_event(
    db_path: Path,
    *,
    dealer_id: int,
    valuation_id: int | None,
    event_type: str,
    payload: dict[str, Any],
    status: str,
    response_text: str = "",
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO crm_events (
                dealer_id, valuation_id, event_type, payload_json,
                status, response_text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dealer_id,
                valuation_id,
                event_type,
                _json(payload),
                status,
                response_text[:1000],
                utc_now(),
            ),
        )
        conn.commit()


def list_crm_events(db_path: Path, dealer_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM crm_events
            WHERE dealer_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (dealer_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def list_data_source_status(db_path: Path) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT source, region, COUNT(*) AS rows_count,
                   MAX(captured_at) AS latest_capture,
                   ROUND(AVG(confidence), 2) AS average_confidence
            FROM market_snapshots
            GROUP BY source, region
            ORDER BY latest_capture DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

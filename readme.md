# AutoTrade Pro

AutoTrade Pro is a white-label trade-in valuation SaaS prototype for automotive dealerships. It replaces the previous kiln app with a public customer valuation journey, incentive reveal flow, appointment booking, CRM event logging, dealer branding controls, and a pluggable valuation data layer.

## What Is Included

- Public dealer app at `/d/<dealer-slug>` with VIN, mileage, condition, photo upload, incentive, valuation, appointment, and confirmation steps.
- Admin dashboard at `/admin` for leads, dealer branding, incentives, data-source status, CRM events, and CSV export.
- Database schema for dealers, incentives, valuations, photos, customers, appointments, market snapshots, CRM events, and audit events. Docker and Railway run on Postgres; SQLite is only an intentional local/test fallback.
- NHTSA vPIC VIN decoder integration with a safe fallback for manual entry.
- Market data abstraction for licensed Manheim/J.D. Power/Black Book-style feeds plus dealer CSV imports and seeded demo South Florida snapshots.
- Valuation engine with condition-first weighting and a hard maximum offer cap at 95% of retail market value by default.
- Optional one-shot or background worker for market snapshot refresh.

## Data Source Notes

The app is wired for real integrations, but not all valuation sources are public APIs:

- NHTSA vPIC is public and used for VIN decoding: https://vpic.nhtsa.dot.gov/api/Home/Index
- Manheim MMR is a licensed wholesale valuation source; API clients are directed to Cox Automotive data syndication: https://site.manheim.com/en/help/mmr.html
- J.D. Power/ChromeData offers vehicle description and valuation products through commercial data services: https://www.jdpower.com/business/features-price-specs
- Black Book offers retail listings and custom trade-value APIs through commercial licensing: https://www.blackbook.com/api/

Without licensed keys, AutoTrade Pro runs with seeded demo market snapshots and a deterministic fallback estimator so dealers can demo the full workflow.

## Run Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m autotrade_pro
```

Open:

- Public app: http://localhost:5000/
- Admin: http://localhost:5000/admin
- Healthcheck: http://localhost:5000/healthz
- Default admin password: `AutoTrade123!`

## Railway

This repo is ready for Railway GitHub deployments:

- `railway.toml` selects the Dockerfile builder and checks `/healthz`.
- The Dockerfile runs Gunicorn on Railway's injected `$PORT`.
- Production dependencies stay in `requirements.txt`; test/dev tooling lives in `requirements-dev.txt` and `pyproject.toml` extras.

Set production secrets in Railway variables, especially:

- `DATABASE_URL` from a Railway Postgres service, or `AUTOTRADE_DATABASE_URL` if you want to override it
- `AUTOTRADE_SECRET_KEY`
- `AUTOTRADE_ADMIN_PASSWORD`
- Optional CRM, SMTP, SMS, and licensed valuation feed variables from `.env.example`

Railway starts the app with `AUTOTRADE_REQUIRE_DATABASE_URL=1`, so production startup fails if a Postgres URL is missing instead of silently using SQLite. If you attach a Railway volume, set `AUTOTRADE_DATA_DIR=/app/data` so uploads persist across deploys; leads, appointments, and market imports should live in Postgres.

## AI Valuation Assist

Admins can optionally save an OpenAI API key in the dealer settings panel. When enabled, AutoTrade Pro sends the deterministic valuation context and uploaded vehicle photos to the OpenAI Responses API for a structured pricing sanity check and photo-condition digest. The AI review is advisory: offer changes are capped by the dealer's AI adjustment limit and still cannot exceed the retail cap.

## Docker

```bash
docker-compose up --build
```

The Compose stack starts a Postgres 16 service and points the app at it with `DATABASE_URL`.

For live reload:

```bash
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

## Configuration

Common environment variables:

- `AUTOTRADE_DATA_DIR`: runtime data directory, default `data`
- `AUTOTRADE_DB_FILE`: SQLite file, default `autotrade.db`
- `DATABASE_URL`: Railway Postgres connection URL, used automatically when present
- `AUTOTRADE_DATABASE_URL`: explicit Postgres override; takes precedence over `DATABASE_URL`
- `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`: accepted when a platform exposes Postgres as component variables instead of a URL
- `AUTOTRADE_REQUIRE_DATABASE_URL`: set to `1` to forbid SQLite fallback
- `AUTOTRADE_DB_CONNECT_RETRIES` and `AUTOTRADE_DB_CONNECT_RETRY_SECONDS`: Postgres startup retry tuning
- `AUTOTRADE_ADMIN_PASSWORD`: admin dashboard password
- `AUTOTRADE_DEFAULT_DEALER`: default dealer slug, default `south-florida-demo`
- `AUTOTRADE_MARKET_REGION`: valuation region, default `south_florida`
- `AUTOTRADE_MARKET_FEED_CSV`: optional CSV import path
- `AUTOTRADE_MANHEIM_API_BASE` and `AUTOTRADE_MANHEIM_API_KEY`: optional licensed wholesale feed
- `AUTOTRADE_JD_POWER_API_BASE` and `AUTOTRADE_JD_POWER_API_KEY`: optional licensed comparable feed
- `AUTOTRADE_BLACK_BOOK_API_BASE` and `AUTOTRADE_BLACK_BOOK_API_KEY`: optional licensed comparable feed
- `AUTOTRADE_CRM_WEBHOOK_URL`: fallback CRM webhook URL
- `AUTOTRADE_SMTP_*`: optional confirmation email delivery
- `AUTOTRADE_SMS_WEBHOOK_URL`: optional SMS webhook
- `AUTOTRADE_ENABLE_WORKER=1`: starts periodic CSV market refresh in-process

OpenAI settings are managed in the admin UI per dealer so different stores can use different keys, models, and adjustment limits.

## Database Bootstrap

The app initializes and migrates its database on startup, similar to the kiln server bootstrap flow. It uses Postgres when `DATABASE_URL` or `AUTOTRADE_DATABASE_URL` is present. SQLite is only a local fallback when `AUTOTRADE_REQUIRE_DATABASE_URL` is unset or `0`. To run initialization directly without starting the web server:

```bash
python scripts/init_db.py
```

This creates the runtime data directory, applies the current schema, seeds the default demo dealer/incentives/market snapshots, and prints row counts. Use `--refresh-market` to import `AUTOTRADE_MARKET_FEED_CSV` in the same pass, or `--json` for deploy-script friendly output.

## Dealer Market CSV

The built-in seed data is intentionally tiny demo data. For production, import a licensed KBB/auction/retail market export with thousands or millions of rows. The importer accepts these CSV columns:

```text
year,make,model,trim,region,source,retail_value,wholesale_value,sample_size,days_supply,confidence,captured_at
```

Kelley Blue Book-style aliases are accepted too, including `trade_in_value`, `trade_value`, `typical_listing_value`, `typical_listing_price`, `fair_market_value`, and `private_party_value`.

Import from the admin Data Sources panel, or run a bulk import from the shell:

```bash
python scripts/import_market_data.py /path/to/kbb-export.csv --source kelley_blue_book_production --replace-source
```

For Railway, place the CSV on a mounted volume or provide it during a deploy task, then run the same script with `AUTOTRADE_DATA_DIR=/app/data`. The older env-driven worker path still works for scheduled imports:

```bash
export AUTOTRADE_MARKET_FEED_CSV=/app/data/market-feed.csv
python scripts/refresh_market_data.py
```

## Tests

```bash
pytest
```

After `pip install -e ".[dev]"`, plain `pytest` works because the repo is installed as an editable package. `make test` runs the same suite.

## Importable Entrypoints

This repo is ready for common import/deploy flows:

- Python package import: `from autotrade_pro import create_app`
- Module run: `python -m autotrade_pro`
- Console script: `autotrade-pro`
- WSGI import: `wsgi:app`
- Procfile: `web: gunicorn wsgi:app --bind 0.0.0.0:$PORT`

## Packaging

```bash
make zip
```

The zip excludes `.git`, virtual environments, runtime data, and existing zip archives.

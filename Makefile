PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
VENV_PYTHON := $(VENV)/bin/python

.PHONY: setup run test init-db refresh-market real-trade-fixture zip

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -e ".[dev]"

run:
	FLASK_DEBUG=1 $(VENV_PYTHON) -m autotrade_pro

test:
	$(VENV_PYTHON) -m pytest

init-db:
	$(VENV_PYTHON) scripts/init_db.py

refresh-market:
	$(VENV_PYTHON) scripts/refresh_market_data.py

real-trade-fixture:
	$(VENV_PYTHON) scripts/build_real_trade_in_fixture.py

zip:
	rm -f autotrade-pro.zip
	zip -r autotrade-pro.zip server.py wsgi.py pyproject.toml requirements.txt requirements-dev.txt Procfile runtime.txt README.md readme.md Dockerfile railway.toml docker-compose.yml docker-compose.dev.yml Makefile .env.example .python-version .gitignore .dockerignore .github src scripts tests -x "__pycache__/*" "*/__pycache__/*" "*.egg-info/*"

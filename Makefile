PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
VENV_PYTHON := $(VENV)/bin/python

.PHONY: setup run test refresh-market zip

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -r requirements.txt

run:
	FLASK_DEBUG=1 $(VENV_PYTHON) server.py

test:
	PYTHONPATH=src $(VENV_PYTHON) -m pytest

refresh-market:
	PYTHONPATH=src $(VENV_PYTHON) scripts/refresh_market_data.py

zip:
	rm -f autotrade-pro.zip
	zip -r autotrade-pro.zip server.py requirements.txt Dockerfile docker-compose.yml docker-compose.dev.yml Makefile readme.md .gitignore src scripts tests -x "__pycache__/*" "*/__pycache__/*"

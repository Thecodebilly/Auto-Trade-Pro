# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV AUTOTRADE_REQUIRE_DATABASE_URL=1

WORKDIR /app

COPY pyproject.toml README.md requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY wsgi.py .
COPY src/ src/
COPY scripts/ scripts/
RUN pip install --no-cache-dir --no-deps .

RUN mkdir -p /app/data

EXPOSE 5000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers ${WEB_CONCURRENCY:-2} --timeout 120 wsgi:app"]

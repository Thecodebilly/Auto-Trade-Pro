# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY src/ src/
COPY scripts/ scripts/

RUN mkdir -p /app/data

EXPOSE 5000

CMD ["python", "server.py"]

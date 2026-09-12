# syntax=docker/dockerfile:1
# ─── Oracle-1001 / Sentinel — Multi-stage production image ───────────────────
# Python 3.11-slim · public HTTP via DASHBOARD_PORT (default 8765) · WAL SQLite
# Build: docker build -t oracle1001-sentinel:latest .
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Builder — compile / wheel heavy scientific stack ────────────────
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        libgomp1 \
        libffi-dev \
        libssl-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m pip install --upgrade pip setuptools wheel \
    && mkdir -p /wheels \
    && python -m pip wheel --wheel-dir=/wheels -r requirements.txt \
    && python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && find /opt/venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /opt/venv -name "*.pyc" -delete 2>/dev/null || true

# ── Stage 2: Runner — minimal runtime ────────────────────────────────────────
FROM python:3.11-slim AS runner

LABEL org.opencontainers.image.title="Oracle-1001 Sentinel" \
      org.opencontainers.image.description="AIS ingest + TTF analytics + Sentinel HUD" \
      org.opencontainers.image.version="2026.09"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    PORT=8765 \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8765 \
    ASSETS_7000_DIR=/app/assets/7000 \
    SENTINEL_DB_PATH=/app/история1/sentinel_ais.db \
    OUTPUT_DIR=/app/output \
    DATA_DIR=/app/data \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libgomp1 \
        sqlite3 \
        bash \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-password --gecos "" --uid 10001 sentinel \
    && mkdir -p \
        /app/output/api/v1 \
        /app/output/js \
        /app/output/assets/top10 \
        /app/assets/7000 \
        /app/data \
        "/app/история1" \
        /app/logs \
        /app/docker

COPY --from=builder /opt/venv /opt/venv
COPY --chown=sentinel:sentinel . .
COPY docker/entrypoint.sh /app/docker/entrypoint.sh

RUN sed -i 's/\r$//' /app/docker/entrypoint.sh \
    && sed -i 's/\r$//' /app/scripts/ais_ingest_healthcheck.sh \
    && chmod +x /app/docker/entrypoint.sh /app/scripts/ais_ingest_healthcheck.sh \
    && chown -R sentinel:sentinel /app

USER sentinel

EXPOSE 8765

# OCI HEALTHCHECK — must return 200 within 5s timeout
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS --max-time 5 "http://127.0.0.1:${DASHBOARD_PORT:-8765}/output/api/v1/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["python", "scripts/serve_dashboard.py", "--host", "0.0.0.0"]

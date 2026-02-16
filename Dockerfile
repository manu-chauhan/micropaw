# MicroClaw — Multi-stage Dockerfile
# Base: slim Python image with core deps
# Browser: adds Playwright + Chromium for web_browse tool

# ── Base stage ──────────────────────────────────────────────
FROM python:3.12-slim AS base

WORKDIR /app

# System deps for httpx/aiosqlite
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ src/

# Install core dependencies only
RUN pip install --no-cache-dir .

# Non-root user for security
RUN useradd -m -r microclaw && \
    mkdir -p /home/microclaw/.microclaw && \
    chown -R microclaw:microclaw /home/microclaw/.microclaw

USER microclaw

ENV MICROCLAW_HOME=/home/microclaw/.microclaw

ENTRYPOINT ["microclaw"]
CMD ["start"]

# ── Browser stage (adds Playwright + Chromium) ─────────────
FROM base AS browser

USER root

# Install Playwright system deps + Chromium
RUN pip install --no-cache-dir ".[browser]" && \
    playwright install --with-deps chromium

USER microclaw

ENTRYPOINT ["microclaw"]
CMD ["start"]

# ── All extras stage ───────────────────────────────────────
FROM browser AS full

USER root

# Install all optional dependencies
RUN pip install --no-cache-dir ".[all]"

USER microclaw

ENTRYPOINT ["microclaw"]
CMD ["start"]

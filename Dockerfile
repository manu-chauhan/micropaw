# MicroPaw — Multi-stage Dockerfile
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
RUN useradd -m -r micropaw && \
    mkdir -p /home/micropaw/.micropaw && \
    chown -R micropaw:micropaw /home/micropaw/.micropaw

USER micropaw

ENV MICROPAW_HOME=/home/micropaw/.micropaw

ENTRYPOINT ["micropaw"]
CMD ["start"]

# ── Browser stage (adds Playwright + Chromium) ─────────────
FROM base AS browser

USER root

# Install Playwright system deps + Chromium
RUN pip install --no-cache-dir ".[browser]" && \
    playwright install --with-deps chromium

USER micropaw

ENTRYPOINT ["micropaw"]
CMD ["start"]

# ── All extras stage ───────────────────────────────────────
FROM browser AS full

USER root

# Install all optional dependencies
RUN pip install --no-cache-dir ".[all]"

USER micropaw

ENTRYPOINT ["micropaw"]
CMD ["start"]

# ─── Phase 4: Production Dockerfile ──────────────────────────────
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps for psycopg2, pillow (reportlab), and healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libjpeg-dev \
        zlib1g-dev \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python deps first (better layer caching)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy source
COPY . .

# Ensure runtime dirs exist
RUN mkdir -p /app/logs /app/assets/logos /app/assets/products /app/uploads

# Non-root user
RUN useradd --create-home --shell /bin/bash botuser \
    && chown -R botuser:botuser /app
USER botuser

# tini as PID 1 → clean signal handling for python-telegram-bot
ENTRYPOINT ["/usr/bin/tini", "--"]

CMD python webhook_server.py & exec python bot.py

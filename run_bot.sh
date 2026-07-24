#!/bin/bash
# Telegram Store Bot Runner
set -e

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BOT_DIR"

# Create venv if missing
if [ ! -d "venv" ]; then
    echo "[SETUP] Creating virtual environment..."
    python3 -m venv venv
fi

# Install/upgrade dependencies
echo "[SETUP] Installing dependencies..."
venv/bin/pip install -q -r requirements.txt

echo "[BOT] Starting bot..."
exec venv/bin/python3 bot.py

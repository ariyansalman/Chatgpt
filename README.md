# Telegram Store Bot

A professional Telegram-based digital product store built with Python, python-telegram-bot v20, SQLAlchemy, and Flask.

## Overview

Telegram Store Bot provides a complete in-Telegram shopping and administration system for selling and automatically delivering digital products. Customers can browse products, manage a wallet, place orders, receive digital inventory, and access order history directly from Telegram.

## Core Features

- Product catalog with categories and subcategories
- Digital product and inventory management
- Automatic key and account delivery
- Internal user wallet and transaction history
- Cart and checkout system
- Order lifecycle and order history
- Manual and crypto payment support
- Referral system
- Loyalty points and user badges
- Product reviews
- Dispute and support workflows
- Reseller, supplier, and batch inventory tracking
- Delivery queue with retry support
- Admin Control Center
- Analytics and audit logging
- Backup services
- PostgreSQL and SQLite support
- Monitoring configuration for Prometheus and Grafana

## Admin Management

The Telegram admin interface is designed to manage store operations without repeatedly editing source code. Admin tools cover products, categories, inventory, orders, users, payments, broadcasts, store configuration, analytics, and operational workflows.

## Technology Stack

- Python
- python-telegram-bot v20
- SQLAlchemy
- Flask
- PostgreSQL / SQLite
- ReportLab
- Prometheus / Grafana monitoring configuration

## Project Structure

```text
bot_src/
├── bot.py
├── config/
├── database/
├── handlers/
├── services/
├── utils/
├── tests/
├── migrations/
├── monitoring/
└── scripts/
```

## Configuration

Runtime configuration and credentials should be supplied through environment variables. Do not commit `.env`, database files, backups, API keys, bot tokens, payment credentials, or other secrets to Git.

Common configuration includes:

- `BOT_TOKEN`
- `ADMIN_TELEGRAM_ID`
- `DATABASE_URL`
- Payment provider credentials when enabled
- Monitoring and backup configuration when enabled

Review `.env.example` and the project configuration modules before deployment.

## Installation

```bash
git clone https://github.com/ariyansalman/telegramshopbot.git
cd telegramshopbot/telegram-bot/bot_src
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Configure the required environment variables, then run:

```bash
python3 bot.py
```

## Database

For production use, PostgreSQL is recommended so user balances, orders, inventory, transactions, and store history remain separate from the VPS filesystem. Database migrations and backups should be handled carefully before deploying code updates.

## Deployment

The bot can be hosted on a Linux VPS. Keep application code in GitHub and persistent business data in the configured database. Code updates should not require deleting or recreating the production database.

Before each production update:

1. Create a database backup.
2. Review database migrations.
3. Pull the latest code.
4. Install updated dependencies if required.
5. Run tests.
6. Restart the bot service.
7. Check logs and health monitoring.

## Security

Never commit bot tokens, PostgreSQL passwords, GitHub Personal Access Tokens, payment API secrets, or private `.env` files. Revoke and rotate any credential that has been exposed in a screenshot, chat, log, or public repository.

## Repository

Maintained by Ariyan Salman.

This repository contains the source code and operational components for the Telegram Store Bot project.

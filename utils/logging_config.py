"""Centralized logging setup: console + rotating file, structured format.

Usage (once at process start):
    from utils.logging_config import setup_logging
    setup_logging()
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))  # 5 MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))

_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | "
    "%(filename)s:%(lineno)d | %(message)s"
)


def setup_logging() -> logging.Logger:
    """Configure root logger with console + rotating file handlers."""
    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)

    # Wipe any pre-existing handlers (e.g. basicConfig) so we don't duplicate.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(LOG_LEVEL)
    root.addHandler(console)

    # Rotating file — general log
    app_file = RotatingFileHandler(
        os.path.join(LOG_DIR, "bot.log"),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    app_file.setFormatter(formatter)
    app_file.setLevel(LOG_LEVEL)
    root.addHandler(app_file)

    # Rotating file — errors only
    err_file = RotatingFileHandler(
        os.path.join(LOG_DIR, "errors.log"),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    err_file.setFormatter(formatter)
    err_file.setLevel(logging.ERROR)
    root.addHandler(err_file)

    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info("Logging initialized (level=%s, dir=%s)", LOG_LEVEL, LOG_DIR)
    return logger

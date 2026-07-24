"""Lightweight performance logging.

Two building blocks:

- ``@perf_track("handler_name")`` — wrap an async handler; logs
  ``PERF | handler_name | 0.342s`` after it returns.
- ``with perf_step("step_name"):`` — time a step inside a handler (e.g. one
  DB call) and log ``PERF | step_name | 0.052s``. If the step takes longer
  than ``SLOW_DB_THRESHOLD_SECONDS`` it's also logged as
  ``SLOW DB | step_name | 1.421s`` so slow queries stand out in the logs
  without needing DEBUG-level verbosity everywhere.

Never logs secrets (DATABASE_URL, tokens, passwords) — only a name and a
duration, by design, so it's safe to leave on in production without
scrubbing.
"""

from __future__ import annotations

import functools
import logging
import time
from contextlib import contextmanager

logger = logging.getLogger("perf")

SLOW_DB_THRESHOLD_SECONDS = 1.0


@contextmanager
def perf_step(name: str):
    t0 = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - t0
        logger.info("PERF | %s | %.3fs", name, elapsed)
        if elapsed > SLOW_DB_THRESHOLD_SECONDS:
            logger.warning("SLOW DB | %s | %.3fs", name, elapsed)


def perf_track(name: str):
    """Decorator for async Telegram handlers."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                return await func(*args, **kwargs)
            finally:
                elapsed = time.monotonic() - t0
                logger.info("PERF | %s | %.3fs", name, elapsed)
                if elapsed > SLOW_DB_THRESHOLD_SECONDS:
                    logger.warning("SLOW DB | %s | %.3fs", name, elapsed)
        return wrapper
    return decorator

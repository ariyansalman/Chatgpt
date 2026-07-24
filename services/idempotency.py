"""Section 16 — DB-backed payment idempotency.

We record every completed payment event by ``(source, external_ref)`` and
rely on a UNIQUE index to reject duplicate processing atomically. Callers
wrap their side-effects in ``with claim(source, ref) as ok:`` — when
``ok`` is False the payment was already processed and the block is skipped.

This lives alongside existing status checks; it is defense in depth, not a
replacement for row-locking during wallet/stock mutations.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.exc import IntegrityError

from database import get_db_session
from database.models import PaymentIdempotency

logger = logging.getLogger(__name__)


def claim_locked(session, source: str, external_ref: str) -> bool:
    """Core claim logic — operates on the CALLER's session/transaction.

    Use this instead of ``claim()`` when already inside an open
    ``get_db_session()`` block (e.g. a loop that holds other loaded ORM
    objects, or a handler that needs to keep using the same session
    afterward). ``get_db_session()`` is backed by a ``scoped_session``, so
    calling ``claim()`` (which opens and CLOSES its own nested session)
    from inside an already-open session would close the shared underlying
    session out from under the caller, detaching any objects it still
    holds. This uses a SAVEPOINT so a duplicate-claim IntegrityError only
    rolls back the claim insert, not the caller's whole transaction.

    Returns True if the claim was won (caller should proceed), False if
    this reference was already claimed (caller should skip / no-op).
    """
    if not external_ref:
        logger.warning("payment idempotency: empty external_ref for %s", source)
        return True
    row = PaymentIdempotency(source=source, external_ref=external_ref)
    nested = session.begin_nested()
    session.add(row)
    try:
        nested.commit()
    except IntegrityError:
        nested.rollback()
        return False
    return True


@contextmanager
def claim(source: str, external_ref: str) -> Iterator[bool]:
    """Attempt to claim a payment reference. Yields True exactly once.

    Opens its own session — use ``claim_locked`` instead when already
    inside an existing ``get_db_session()`` block.
    """
    if not external_ref:
        # Nothing to dedupe on — let caller proceed but log.
        logger.warning("payment idempotency: empty external_ref for %s", source)
        yield True
        return
    with get_db_session() as s:
        won = claim_locked(s, source, external_ref)
        if not won:
            yield False
            return
    yield True

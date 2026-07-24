"""Single, standardized layout for every admin-facing notification.

Every admin alert in the project — orders, payments, users, coupons,
inventory, support, system — renders through :func:`render` so they all
look and read the same way:

    {icon} <b>{title}</b>

    {Label}: {value}
    {Label}: {value}

    🕒 {timestamp}

Rules baked into this module (see the admin-notifications redesign spec):
  • One layout, no per-event variations.
  • No dashed/line separators.
  • No repeated section headers — a flat list of labeled fields.
  • Fields with no value are dropped automatically, so callers never end
    up printing "Reason: —" style noise or duplicate info.
  • Only one timestamp line, never both UTC and local.

This module is presentation-only. It never touches business logic,
database state, or decides *whether* a notification is sent — callers
keep doing that exactly as before and just hand values to ``render()``
to get back the text.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Tuple

FieldList = Iterable[Tuple[str, object]]


def render(icon: str, title: str, fields: FieldList,
           timestamp: Optional[str] = None) -> str:
    """Build one standardized admin notification message.

    Args:
        icon: a single leading emoji for the event category.
        title: short event title, e.g. "New Order".
        fields: ordered (label, value) pairs. Entries whose value is
            ``None`` or ``""`` are skipped automatically.
        timestamp: optional pre-formatted timestamp string. Omit to leave
            the timestamp off entirely (e.g. when an admin has disabled it).
    """
    lines = [f"{icon} <b>{title}</b>", ""]
    for label, value in fields:
        if value is None or value == "":
            continue
        lines.append(f"{label}: {value}")
    if timestamp:
        lines.append("")
        lines.append(f"🕒 {timestamp}")
    return "\n".join(lines)


def utc_now_str() -> str:
    """Single canonical timestamp format used across all admin notifications."""
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

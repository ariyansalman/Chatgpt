"""
Global Delivery Message Renderer — V41.

Template-based delivery message system. Admins edit the template via the
admin panel; the renderer fills in real values at delivery time.

Stored in ``Settings.delivery_message_template`` (nullable TEXT).
When NULL the ``DEFAULT_TEMPLATE`` is used so the store always looks good
out of the box.

Supported placeholders
──────────────────────
  {order_id}       receipt / order display number (ORD-YYYYMMDD-NNNNNN)
  {product_name}   product name
  {quantity}       quantity purchased
  {amount}         formatted amount paid (e.g. $9.99)
  {purchase_time}  purchase date/time (e.g. "27 Jul 2026 • 14:30 UTC")
  {email}          email from delivery asset
  {password}       password from delivery asset
  {twofa}          2FA / OTP code from delivery asset
  {recovery}       recovery email (4-field pipe or JSON "recovery" key)

Rendering rules
───────────────
  • A line whose EVERY placeholder is blank/null is silently dropped.
  • Consecutive blank lines are collapsed to a single blank line.
  • Result is stripped of leading/trailing whitespace.
  • Separator lines and text with no placeholders are always kept.

accdel_show_* settings
───────────────────────
  When the admin disables one of the "accdel_show_*" toggles the
  corresponding fields are suppressed at render time:
    accdel_show_order_summary  → hides {order_id}, {amount}, {purchase_time}
    accdel_show_product_info   → hides {product_name}
    accdel_show_purchase_time  → hides {purchase_time}
    accdel_show_quantity       → hides {quantity}
    accdel_show_2fa            → hides {twofa}
  Lines whose only placeholders are suppressed are dropped automatically by
  the existing blank-line rule — no template changes needed.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Default template
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_TEMPLATE = (
    "✅ Payment Successful\n"
    "\n"
    "🆔 Order ID: {order_id}\n"
    "🛒 Product: {product_name}\n"
    "🔢 Qty: {quantity}\n"
    "💰 Paid: {amount}\n"
    "🕒 {purchase_time}\n"
    "\n"
    "━━━━━━━━━━━━━━\n"
    "\n"
    "✅ Product Delivered\n"
    "\n"
    "📧 Email: {email}\n"
    "🔑 Password: {password}\n"
    "🔐 2FA: {twofa}"
)

# ─────────────────────────────────────────────────────────────────────────────
# Placeholder extraction
# ─────────────────────────────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# Sample values used during admin preview
_SAMPLE_VALUES: Dict[str, str] = {
    "order_id":      "ORD-20260727-100042",
    "product_name":  "Netflix Premium 1 Month",
    "quantity":      "1",
    "amount":        "$14.99",
    "purchase_time": "27 Jul 2026 • 14:30 UTC",
    "email":         "user@example.com",
    "password":      "P@ssw0rd!23",
    "twofa":         "482913",
    "recovery":      "backup@example.com",
}


def _extract_placeholders_in_line(line: str) -> List[str]:
    """Return placeholder names found in a single line (may be empty)."""
    return _PLACEHOLDER_RE.findall(line)


# ─────────────────────────────────────────────────────────────────────────────
# Delivery asset parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_delivery_asset(raw: Optional[str]) -> Dict[str, str]:
    """Parse a delivered asset string into a flat field dict.

    Handles four formats in order:
      1. JSON object  ``{"email": ..., "password": ..., "twofa": ...}``
         Keys are lower-cased; "2fa" is normalised to "twofa".
      2. Pipe-delimited (3-field)  ``email|password|twofa``
      3. Pipe-delimited (4-field)  ``email|password|recovery|twofa``
         Field order matches ``format_account_delivery`` in inventory_import.py.
      4. Raw key / single value — no named fields extracted.

    The pipe-delimited 4-field order is email|password|RECOVERY|TWOFA,
    consistent with how ``format_account_delivery()`` parses it.
    """
    fields: Dict[str, str] = {}
    if not raw or not raw.strip():
        return fields
    text = raw.strip()

    # 1. JSON
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if v is not None:
                        norm_key = str(k).lower().replace("-", "_")
                        # normalise "2fa" → "twofa"
                        if norm_key == "2fa":
                            norm_key = "twofa"
                        fields[norm_key] = str(v)
                return fields
        except (ValueError, TypeError):
            pass

    # 2 & 3. Pipe-delimited
    if "|" in text:
        parts = [p.strip() for p in text.split("|")]
        # Always map the first two fields
        if len(parts) >= 1 and parts[0]:
            fields["email"] = parts[0]
        if len(parts) >= 2 and parts[1]:
            fields["password"] = parts[1]
        if len(parts) == 3:
            # 3-field: email|password|twofa
            if parts[2]:
                fields["twofa"] = parts[2]
        elif len(parts) >= 4:
            # 4-field: email|password|recovery|twofa
            # Field 3 = recovery email, Field 4 = 2FA / recovery code
            if parts[2]:
                fields["recovery"] = parts[2]
            if parts[3]:
                fields["twofa"] = parts[3]
        return fields

    # 4. Raw value — no named fields; callers handle delivery block directly
    return fields


# ─────────────────────────────────────────────────────────────────────────────
# Core renderer
# ─────────────────────────────────────────────────────────────────────────────

class _BlankOnMissing(dict):
    """dict subclass: str.format_map() returns "" instead of raising KeyError."""
    def __missing__(self, key: str) -> str:  # noqa: D105
        return ""


def render_template(template: str, fields: Dict[str, Any]) -> str:
    """Render *template* with *fields*, hiding lines whose only values are blank.

    Rules applied per line:
      • Lines with NO placeholders → always kept.
      • Lines with at least one placeholder where the resolved value is
        non-empty → kept (blank-on-missing for the empty ones).
      • Lines where EVERY placeholder resolves to "" → dropped silently.

    Post-processing:
      • Three or more consecutive blank lines → two blank lines.
      • Leading/trailing blank lines stripped.
    """
    # Normalise None values to "" so format_map never crashes
    safe_fields = _BlankOnMissing({
        k: ("" if v is None else str(v))
        for k, v in fields.items()
    })

    output_lines: List[str] = []
    for raw_line in template.splitlines():
        ph_names = _extract_placeholders_in_line(raw_line)
        if ph_names:
            # Check whether all placeholder values for this line are empty
            all_empty = all(safe_fields.get(ph, "") == "" for ph in ph_names)
            if all_empty:
                continue  # drop this line silently
        # Render with blanks-on-missing for any remaining missing keys
        try:
            rendered = raw_line.format_map(safe_fields)
        except (KeyError, ValueError):
            rendered = raw_line  # keep as-is on any format error
        output_lines.append(rendered)

    # Collapse 3+ consecutive blank lines to 2 blank lines
    collapsed: List[str] = []
    blank_run = 0
    for line in output_lines:
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 2:
                collapsed.append(line)
        else:
            blank_run = 0
            collapsed.append(line)

    # Strip leading/trailing blank lines
    while collapsed and collapsed[0].strip() == "":
        collapsed.pop(0)
    while collapsed and collapsed[-1].strip() == "":
        collapsed.pop()

    return "\n".join(collapsed)


# ─────────────────────────────────────────────────────────────────────────────
# accdel_show_* settings helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_accdel_show_settings() -> Dict[str, bool]:
    """Return the admin-configured accdel_show_* visibility toggles.

    Defaults to all-visible so that disabling the toggle is the opt-out
    action and existing deployments keep showing all fields.
    """
    defaults = {
        "order_summary":  True,
        "product_info":   True,
        "purchase_time":  True,
        "quantity":       True,
        "twofa":          True,
    }
    try:
        from utils.bot_config import cfg as _cfg
        return {
            "order_summary":  _cfg.get_bool("accdel_show_order_summary",  True),
            "product_info":   _cfg.get_bool("accdel_show_product_info",    True),
            "purchase_time":  _cfg.get_bool("accdel_show_purchase_time",   True),
            "quantity":       _cfg.get_bool("accdel_show_quantity",         True),
            "twofa":          _cfg.get_bool("accdel_show_2fa",              True),
        }
    except Exception:
        logger.debug("delivery_message_renderer: could not read accdel_show_* settings",
                     exc_info=True)
        return defaults


def _apply_show_settings(fields: Dict[str, str],
                         show: Dict[str, bool]) -> Dict[str, str]:
    """Zero-out fields whose accdel_show_* toggle is OFF.

    The blank-line rule in ``render_template`` then silently drops any line
    that only contained those placeholders — no template changes needed.
    """
    result = dict(fields)
    if not show.get("order_summary", True):
        # Suppress the entire order-summary block placeholders
        for key in ("order_id", "amount", "purchase_time"):
            result[key] = ""
    if not show.get("product_info", True):
        result["product_name"] = ""
    if not show.get("purchase_time", True):
        # purchase_time may be suppressed independently of the full summary
        result["purchase_time"] = ""
    if not show.get("quantity", True):
        result["quantity"] = ""
    if not show.get("twofa", True):
        result["twofa"] = ""
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Global template store/fetch
# ─────────────────────────────────────────────────────────────────────────────

def get_global_template() -> str:
    """Return the admin-configured global delivery template (or DEFAULT_TEMPLATE)."""
    try:
        from database import get_db_session
        from database.models import Settings
        with get_db_session() as s:
            row = s.query(Settings).first()
            if row and getattr(row, "delivery_message_template", None):
                return row.delivery_message_template
    except Exception:
        logger.debug("delivery_message_renderer: could not fetch template from DB", exc_info=True)
    return DEFAULT_TEMPLATE


def set_global_template(template: Optional[str]) -> None:
    """Persist *template* in Settings.delivery_message_template (None → default)."""
    try:
        from database import get_db_session
        from database.models import Settings
        with get_db_session() as s:
            row = s.query(Settings).first()
            if row is None:
                row = Settings()
                s.add(row)
            row.delivery_message_template = template
            s.commit()
    except Exception:
        logger.exception("delivery_message_renderer: failed to save template")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_delivery_message(
    *,
    order_id: str,
    product_name: str,
    quantity: int,
    amount: str,
    purchase_time: str,
    delivered_asset: Optional[str] = None,
    template: Optional[str] = None,
) -> str:
    """Render the complete customer delivery message.

    Args:
        order_id:        Receipt/order display number.
        product_name:    Product name.
        quantity:        Units purchased.
        amount:          Formatted amount string (e.g. "$14.99").
        purchase_time:   Human-readable purchase timestamp.
        delivered_asset: Raw delivered value (pipe-delimited, JSON, or key).
        template:        Override template; ``None`` → fetch from DB / default.

    Returns:
        Rendered message string ready to send to Telegram.

    Note:
        Admin ``accdel_show_*`` visibility settings are honoured — disabled
        fields are zeroed before rendering so the blank-line rule silently
        removes lines that only contained suppressed placeholders.
    """
    tmpl = template if template is not None else get_global_template()

    # Order-level fields
    fields: Dict[str, str] = {
        "order_id":      order_id,
        "product_name":  product_name,
        "quantity":      str(quantity),
        "amount":        amount,
        "purchase_time": purchase_time,
    }

    # Delivery-level fields (from delivered_asset)
    delivery_fields = parse_delivery_asset(delivered_asset)
    fields.update(delivery_fields)

    # Apply accdel_show_* visibility toggles
    show = _get_accdel_show_settings()
    fields = _apply_show_settings(fields, show)

    return render_template(tmpl, fields)


def render_preview(template: Optional[str] = None) -> str:
    """Render *template* (or the current global template) with sample values.

    Used by the admin panel preview so admins can see how a template looks
    before saving it.
    """
    tmpl = template if template is not None else get_global_template()
    return render_template(tmpl, dict(_SAMPLE_VALUES))

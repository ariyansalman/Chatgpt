"""V17 — Formatted Account Delivery.

Lets an admin define a per-product ``delivery_format_template`` (stored on
``Product.delivery_format_template``) containing ``{placeholder}`` tokens,
e.g.::

    📄 Your Account Details
    ━━━━━━━━━━━━━━
    📧 Email: {email}
    🔑 Password: {password}
    🔐 Recovery Email: {recovery}
    📅 Valid Until: {expiry}
    ━━━━━━━━━━━━━━
    ⚠️ Please change the password after first login.

Underlying inventory (``ProductKey.key_value``) keeps working exactly as
before. This module is purely additive:

  * Legacy stock — a single raw string (a plain key, or the existing
    ``email|password|2fa`` pipe format used by ACCOUNT_LOGIN) — is parsed
    on the fly into a field dict so it can be dropped into a template.
  * New structured stock is stored as a JSON object string, e.g.
    ``{"email": "...", "password": "...", "recovery": "...", "expiry": "..."}``
    so admins can bulk-upload multi-field records that line up 1:1 with
    template placeholders.

Nothing here changes ``ProductKey.key_value``'s column type — it stays a
``Text`` column; JSON is simply one of the strings that can live in it.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# Matches {placeholder_name} tokens — letters, digits, underscore only.
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# Legacy positional field names for pipe-delimited ACCOUNT_LOGIN stock
# (matches services/inventory_import.parse_account_inventory's output order).
_LEGACY_PIPE_FIELDS = ["email", "password", "recovery", "expiry"]

# Sample values used when previewing a template with no real stock yet.
_SAMPLE_VALUES: Dict[str, str] = {
    "email": "user@example.com",
    "password": "P@ssw0rd!23",
    "recovery": "backup@example.com",
    "recovery_email": "backup@example.com",
    "expiry": "2026-12-31",
    "expiry_date": "2026-12-31",
    "valid_until": "2026-12-31",
    "username": "sample_user",
    "pin": "482913",
    "2fa": "482913",
    "code": "ABCD-1234-EFGH",
    "key": "ABCD1-EFGH2-IJKL3",
    "license": "ABCD1-EFGH2-IJKL3",
    "license_key": "ABCD1-EFGH2-IJKL3",
    "link": "https://example.com/redeem/abc123",
    "recovery_code": "9F3K-2QWE-7RTY",
    "profile": "Profile 1",
    "pin_code": "482913",
    # Newly added — common fields for subscription / account-style products.
    "otp": "739284",
    "otp_code": "739284",
    "backup_codes": "1a2b3c, 4d5e6f, 7g8h9i",
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    "api_key": "sk_live_51Hx9k2ExampleKeyXYZ",
    "activation_code": "ACT-2026-XJ4K9",
    "phone": "+8801XXXXXXXXX",
    "phone_number": "+8801XXXXXXXXX",
    "server": "sg1.example-vpn.net",
    "region": "Singapore",
    "plan": "Premium (1 Month)",
    "subscription": "Premium (1 Month)",
    "login_url": "https://example.com/login",
    "url": "https://example.com/login",
    "warranty": "30 Days Replacement Warranty",
    "support": "@YourStoreSupport",
    "note": "Do not share these credentials with anyone.",
    "instructions": "Login and change your password immediately.",
    "renewal_date": "2026-08-15",
    "activation_date": "2026-07-15",
    "order_id": "ORD-100234",
    "device_limit": "1 Device",
    "slots": "1/1",
}


def extract_placeholders(template: Optional[str]) -> List[str]:
    """Return the ordered, de-duplicated list of ``{name}`` tokens in a template."""
    if not template:
        return []
    seen: List[str] = []
    for match in _PLACEHOLDER_RE.finditer(template):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


class _BlankOnMissing(dict):
    """dict subclass so str.format_map() renders "" instead of raising KeyError."""

    def __missing__(self, key):
        return ""


def render_template(template: str, fields: Dict[str, Any]) -> str:
    """Render ``template`` against ``fields``. Missing placeholders render as ''.

    Never raises — a malformed template (e.g. stray ``{``) falls back to
    returning the template unchanged so delivery never hard-fails because of
    an admin typo.
    """
    if not template:
        return ""
    safe_fields = _BlankOnMissing({
        str(k): ("" if v is None else str(v)) for k, v in (fields or {}).items()
    })
    try:
        return template.format_map(safe_fields)
    except Exception:
        return template


def parse_key_value(raw: str, placeholders: Optional[List[str]] = None) -> Dict[str, str]:
    """Parse a ``ProductKey.key_value`` string into a field dict.

    Handles, in order:
      1. JSON object stock (new structured format) — used as-is.
      2. Legacy pipe-delimited stock ("email|password|2fa" style) — mapped
         onto ``placeholders`` positionally, falling back to the historical
         email/password/recovery/expiry field names.
      3. Plain single-value stock — mapped onto the template's first
         placeholder (so a single-field template like "{key}" still works).
    """
    raw = raw or ""
    stripped = raw.strip()

    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return {str(k): ("" if v is None else str(v)) for k, v in parsed.items()}

    if "|" in raw:
        parts = [p.strip() for p in raw.split("|")]
        names = placeholders if placeholders else _LEGACY_PIPE_FIELDS
        fields: Dict[str, str] = {}
        for i, part in enumerate(parts):
            key = names[i] if i < len(names) else f"field{i + 1}"
            fields[key] = part
            fields.setdefault(f"field{i + 1}", part)
        # Also expose legacy names even when a custom placeholder set is used,
        # so templates can mix custom + conventional token names.
        for i, part in enumerate(parts):
            if i < len(_LEGACY_PIPE_FIELDS):
                fields.setdefault(_LEGACY_PIPE_FIELDS[i], part)
        return fields

    if placeholders:
        return {placeholders[0]: raw, "value": raw}
    return {"value": raw}


def render_delivery_message(template: str, raw_key_value: str) -> str:
    """Full pipeline: parse ``raw_key_value`` and render it through ``template``."""
    placeholders = extract_placeholders(template)
    fields = parse_key_value(raw_key_value, placeholders)
    return render_template(template, fields)


def to_storage_value(fields: Dict[str, str]) -> str:
    """Serialize a field dict to the JSON string form stored in ``key_value``."""
    return json.dumps(fields, ensure_ascii=False)


def bulk_parse_structured_lines(text: str, placeholders: List[str],
                                delimiter: str = "|") -> List[str]:
    """Parse admin bulk-upload text into JSON ``key_value`` strings.

    One record per line, fields separated by ``delimiter`` in the same order
    as ``placeholders``. Short lines get the trailing fields blank rather
    than being rejected, so admins don't lose a whole batch to one typo.
    """
    out: List[str] = []
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(delimiter)]
        fields = {ph: (parts[i] if i < len(parts) else "") for i, ph in enumerate(placeholders)}
        out.append(to_storage_value(fields))
    return out


def build_sample_fields(placeholders: List[str]) -> Dict[str, str]:
    """Build plausible sample data for a template preview."""
    fields: Dict[str, str] = {}
    for i, name in enumerate(placeholders):
        key = name.lower()
        if key in _SAMPLE_VALUES:
            fields[name] = _SAMPLE_VALUES[key]
        else:
            fields[name] = f"Sample {name.replace('_', ' ').title()}"
    return fields


def render_preview(template: str) -> str:
    """Render ``template`` with generated sample data — used by the admin preview."""
    placeholders = extract_placeholders(template)
    fields = build_sample_fields(placeholders)
    return render_template(template, fields)

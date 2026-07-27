"""Section 15 — safe duplicate detection for imported inventory.

We store a sha256 fingerprint of a *normalized* key so duplicates can be
detected without ever logging the raw value. Normalization rules are
per-product-type: license keys are case-preserving (they matter), whereas
emails and vouchers get whitespace-trimmed and lowercased.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Dict, Iterable, List, Optional, Tuple

from database.models import ProductType

logger = logging.getLogger(__name__)


def normalize(raw: str, product_type: ProductType | None = None) -> str:
    v = (raw or "").strip()
    if product_type in (ProductType.ACCOUNT_LOGIN, ProductType.VOUCHER,
                        ProductType.REDEEM_LINK):
        return v.lower()
    return v  # keep license keys / files case-sensitive


def fingerprint(raw: str, product_type: ProductType | None = None) -> str:
    n = normalize(raw, product_type)
    return hashlib.sha256(n.encode("utf-8")).hexdigest()


def dedupe_import(lines: Iterable[str],
                  product_type: ProductType | None = None,
                  existing_fps: set[str] | None = None
                  ) -> Tuple[List[Tuple[str, str]], List[str], List[str]]:
    """Split raw import lines into (accepted, duplicates, invalid).

    ``accepted`` items are ``(key_value, fingerprint)`` tuples ready to insert.
    """
    seen: set[str] = set(existing_fps or ())
    accepted: List[Tuple[str, str]] = []
    duplicates: List[str] = []
    invalid: List[str] = []
    for line in lines:
        v = (line or "").strip()
        if not v:
            continue
        if len(v) < 2:
            invalid.append(v)
            continue
        fp = fingerprint(v, product_type)
        if fp in seen:
            duplicates.append(v[:8] + "…")   # never log full value
            continue
        seen.add(fp)
        accepted.append((v, fp))
    return accepted, duplicates, invalid


def parse_account_inventory(text: str) -> List[str]:
    """Parse labelled account blocks while preserving legacy pipe-separated rows."""
    import re
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    nonempty = [line.strip() for line in text.split("\n") if line.strip()]
    if all("|" in line for line in nonempty):
        return nonempty

    items: List[str] = []
    for block in re.split(r"\n\s*\n+", text):
        fields = {}
        for line in block.split("\n"):
            match = re.match(r"^\s*(email|pw|password|2fa)\s*:\s*(.+?)\s*$", line, re.I)
            if match:
                key = match.group(1).lower()
                fields["password" if key == "pw" else key] = match.group(2).strip()
        if fields.get("email") and fields.get("password"):
            value = f"{fields['email']}|{fields['password']}"
            if fields.get("2fa"):
                value += f"|{fields['2fa']}"
            items.append(value)
        else:
            items.extend(line.strip() for line in block.split("\n") if line.strip())
    return items


def _get_accdel_settings() -> dict:
    """Fetch Account/Login delivery display settings from bot_config (with safe defaults)."""
    try:
        from utils.bot_config import cfg as _cfg
        return {
            "compact":  _cfg.get_bool("accdel_compact_layout", False),
            "show_2fa": _cfg.get_bool("accdel_show_2fa", True),
        }
    except Exception:
        return {"compact": False, "show_2fa": True}


def _parse_json_account(value: str) -> Optional[Dict[str, str]]:
    """Try to parse *value* as a JSON account object.

    Returns a dict with lowercase keys (normalising "2fa" → "twofa") if
    *value* is a valid JSON object, otherwise ``None``.
    """
    stripped = (value or "").strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        obj = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    result: Dict[str, str] = {}
    for k, v in obj.items():
        norm_key = str(k).lower().replace("-", "_")
        # normalise "2fa" → "twofa" for consistent template placeholder names
        if norm_key == "2fa":
            norm_key = "twofa"
        result[norm_key] = "" if v is None else str(v)
    return result


def format_account_delivery(value: str, *, compact: bool = False, show_2fa: bool = True) -> str:
    """Format an ACCOUNT_LOGIN inventory value for buyer delivery.

    Accepts three inventory storage formats:
      1. JSON object — ``{"email": ..., "password": ..., "twofa": ...}``
         (new structured format; also handles legacy "2fa" key spelling)
      2. Pipe-delimited — ``email|password`` / ``email|password|twofa`` /
         ``email|password|recovery|twofa`` (four-field format)
      3. Single raw string — returned as-is

    Honors admin-configurable display settings:
      - compact  → condensed single-line format (never exposes raw "|")
      - show_2fa → whether to include 2FA / recovery codes

    Rules
    -----
    • Missing fields are hidden automatically — no "|" separators ever shown.
    • Empty field values are treated as absent.
    """
    # ── 1. JSON format ────────────────────────────────────────────────
    json_fields = _parse_json_account(value)
    if json_fields is not None:
        email    = json_fields.get("email", "")
        password = json_fields.get("password", "")
        twofa    = json_fields.get("twofa", "") or json_fields.get("2fa", "")
        recovery = json_fields.get("recovery", "") or json_fields.get("recovery_email", "")

        if not email and not password:
            # Not a recognisable account — fall back to rendering all fields
            return "\n".join(f"{k}: {v}" for k, v in json_fields.items() if v)

        if compact:
            seg = []
            if email:
                seg.append(email)
            if password:
                seg.append(password)
            if show_2fa and twofa:
                seg.append(f"2FA: {twofa}")
            elif show_2fa and recovery:
                seg.append(f"Recovery: {recovery}")
            return " | ".join(seg)

        lines = []
        if email:
            lines.append(f"📧 Email: {email}")
        if password:
            lines.append(f"🔑 Password: {password}")
        if show_2fa:
            if recovery:
                lines.append(f"📨 Recovery Email: {recovery}")
            if twofa:
                lines.append(f"🔐 2FA: {twofa}")
        return "\n".join(lines)

    # ── 2. Pipe-delimited format ──────────────────────────────────────
    # Field order: email | password | twofa (3-field) or
    #              email | password | recovery | twofa (4-field)
    if "|" in (value or ""):
        parts = [part.strip() for part in value.split("|")]
        if len(parts) >= 2:
            email    = parts[0]
            password = parts[1]
            twofa    = ""
            recovery = ""
            if len(parts) == 3 and parts[2]:
                twofa = parts[2]
            elif len(parts) >= 4:
                recovery = parts[2] if parts[2] else ""
                twofa    = parts[3] if parts[3] else ""

            if compact:
                seg = []
                if email:
                    seg.append(email)
                if password:
                    seg.append(password)
                if show_2fa and twofa:
                    seg.append(f"2FA: {twofa}")
                elif show_2fa and recovery:
                    seg.append(f"Recovery: {recovery}")
                return " | ".join(seg)

            # Default multi-line card format
            lines = []
            if email:
                lines.append(f"📧 Email: {email}")
            if password:
                lines.append(f"🔑 Password: {password}")
            if show_2fa:
                if len(parts) == 3 and twofa:
                    lines.append(f"🔐 2FA: {twofa}")
                elif len(parts) >= 4:
                    if recovery:
                        lines.append(f"📨 Recovery Email: {recovery}")
                    if twofa:
                        lines.append(f"🔐 Recovery Code: {twofa}")
            return "\n".join(lines)

    # ── 3. Single raw string (no pipe, no JSON) ───────────────────────
    return value


# ── Multiple-account delivery formatter ──────────────────────────────────────

# Unicode circled digits ①–⑳ for clean account numbering
_CIRCLED_NUMBERS = [
    "①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
    "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳",
]

_SECTION_SEPARATOR = "━━━━━━━━━━━━━━"


def _account_number(n: int) -> str:
    """Return a circled digit for 1–20, plain (#N) for higher values."""
    if 1 <= n <= len(_CIRCLED_NUMBERS):
        return _CIRCLED_NUMBERS[n - 1]
    return f"({n})"


def _get_accdel_txt_settings() -> dict:
    """Fetch Account/Login TXT delivery settings from bot_config (with safe defaults)."""
    try:
        from utils.bot_config import cfg as _cfg
        return {
            "include_summary":      _cfg.get_bool("accdel_txt_include_summary", True),
            "include_product_name": _cfg.get_bool("accdel_txt_include_product_name", True),
            "divider":              _cfg.get_str("accdel_txt_divider", _SECTION_SEPARATOR) or _SECTION_SEPARATOR,
            "numbering":            _cfg.get_str("accdel_txt_numbering", "circle") or "circle",
            "show_2fa":             _cfg.get_bool("accdel_show_2fa", True),
            "compact":              _cfg.get_bool("accdel_compact_layout", False),
        }
    except Exception:
        return {
            "include_summary": True, "include_product_name": True,
            "divider": _SECTION_SEPARATOR, "numbering": "circle",
            "show_2fa": True, "compact": False,
        }


def _txt_account_number(n: int, numbering: str) -> str:
    """Return account label: circle (①②③) or plain (1,2,3) based on numbering setting."""
    if numbering == "plain":
        return str(n)
    return _account_number(n)


def build_account_delivery_file(
    receipt_number: str,
    product_name: str,
    quantity: int,
    assets: list,
) -> str:
    """Build the UTF-8 content for a .txt account-delivery file.

    Respects admin-configured TXT delivery settings:
      - accdel_txt_include_summary     → include order header block
      - accdel_txt_include_product_name → include product name in header
      - accdel_txt_divider             → section separator style
      - accdel_txt_numbering           → 'circle' (①②③) or 'plain' (1,2,3)
      - accdel_show_2fa                → include/exclude 2FA codes
      - accdel_compact_layout          → condensed single-line per account

    Rules:
    • Pipe separators are never exposed (parsed by format_account_delivery).
    • JSON-format account values are also fully supported.
    • Empty fields are hidden automatically.
    • UTF-8 encoded throughout.
    """
    txt_cfg = _get_accdel_txt_settings()
    divider = txt_cfg["divider"]
    numbering = txt_cfg["numbering"]
    show_2fa = txt_cfg["show_2fa"]
    compact = txt_cfg["compact"]
    include_summary = txt_cfg["include_summary"]
    include_product_name = txt_cfg["include_product_name"]

    header_parts = []
    if include_summary:
        header_parts.append(f"Order ID: {receipt_number}")
        if include_product_name:
            header_parts.append(f"Product: {product_name}")
        header_parts.append(f"Quantity: {quantity}")
    elif include_product_name:
        header_parts.append(f"Product: {product_name}")

    header = "\n".join(header_parts)

    if not assets:
        return (header + "\n") if header else ""

    account_blocks = []
    for i, value in enumerate(assets, start=1):
        num = _txt_account_number(i, numbering)
        formatted = format_account_delivery(value, compact=compact, show_2fa=show_2fa)
        block = f"{num}\n\n{formatted}"
        account_blocks.append(block)

    sep = f"\n\n{divider}\n\n"
    body = sep.join(account_blocks)

    if header:
        return f"{header}\n\n{divider}\n\n{body}\n"
    return f"{body}\n"


def format_multi_account_delivery(values: list) -> str:
    """Format a list of ACCOUNT_LOGIN inventory values for inline buyer delivery.

    Reads ``accdel_compact_layout`` and ``accdel_show_2fa`` from bot_config.

    Single account → plain ``format_account_delivery`` (no number, no separator).
    Multiple accounts → each account prefixed with a circled number and
    separated from the next by the admin-configured inline divider
    (``accdel_txt_divider``; falls back to ``━━━━━━━━━━━━━━``).

    Pipe separators from raw inventory are never exposed to the buyer.
    JSON-format account values are fully supported.
    """
    if not values:
        return ""
    settings = _get_accdel_settings()
    compact = settings["compact"]
    show_2fa = settings["show_2fa"]

    if len(values) == 1:
        return format_account_delivery(values[0], compact=compact, show_2fa=show_2fa)

    # Use the admin-configured divider for the inline format too, so the
    # TXT file and inline message have consistent styling.
    try:
        from utils.bot_config import cfg as _cfg
        divider = _cfg.get_str("accdel_txt_divider", _SECTION_SEPARATOR) or _SECTION_SEPARATOR
    except Exception:
        divider = _SECTION_SEPARATOR

    blocks = []
    for i, value in enumerate(values, start=1):
        formatted = format_account_delivery(value, compact=compact, show_2fa=show_2fa)
        account_block = f"{_account_number(i)}\n{formatted}"
        blocks.append(account_block)

    return ("\n\n" + divider + "\n\n").join(blocks)

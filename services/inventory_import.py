"""Section 15 — safe duplicate detection for imported inventory.

We store a sha256 fingerprint of a *normalized* key so duplicates can be
detected without ever logging the raw value. Normalization rules are
per-product-type: license keys are case-preserving (they matter), whereas
emails and vouchers get whitespace-trimmed and lowercased.
"""
from __future__ import annotations

import hashlib
from typing import Iterable, List, Tuple

from database.models import ProductType


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


def format_account_delivery(value: str) -> str:
    """Format an ACCOUNT_LOGIN inventory value for buyer delivery."""
    parts = [part.strip() for part in (value or "").split("|")]
    if len(parts) < 2:
        return value
    lines = [f"📧 Email: {parts[0]}", f"🔑 Password: {parts[1]}"]
    if len(parts) == 3 and parts[2]:
        lines.append(f"🔐 2FA: {parts[2]}")
    elif len(parts) >= 4:
        if parts[2]:
            lines.append(f"📨 Recovery Email: {parts[2]}")
        if parts[3]:
            lines.append(f"🔐 Recovery Code: {parts[3]}")
    return "\n".join(lines)

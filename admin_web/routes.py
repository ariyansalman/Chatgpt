"""
Admin Web API — Delivery Message Template endpoints.

Registers REST routes under ``/admin/api/`` that the React admin panel
consumes to read, save, preview, and restore the delivery message template.

All business logic lives in ``services.delivery_message_renderer``.
No other service is touched.

Mount by calling ``register_admin_web_routes(app)`` once from the entry-point
(e.g. ``webhook_server.py`` or a dedicated ``admin_server.py``).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from flask import Flask, Blueprint, request, jsonify, send_from_directory
import os

logger = logging.getLogger(__name__)

# ── Blueprint ─────────────────────────────────────────────────────────────────

admin_bp = Blueprint("admin_web", __name__, url_prefix="/admin")

# ── Placeholder validation regex ──────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_KNOWN_VARS = {
    "order_id", "product_name", "quantity", "amount",
    "purchase_time", "email", "password", "twofa",
}

# ── Sample values for preview ─────────────────────────────────────────────────

_SAMPLE_VALUES = {
    "order_id":      "ORD-20260727-100042",
    "product_name":  "Netflix Premium 1 Month",
    "quantity":      "1",
    "amount":        "$14.99",
    "purchase_time": "27 Jul 2026 \u2022 14:30 UTC",
    "email":         "user@example.com",
    "password":      "P@ssw0rd!23",
    "twofa":         "482913",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_template(template: str) -> Optional[str]:
    """Return an error string, or None if the template is valid."""
    if not template or not template.strip():
        return "Template cannot be empty"
    # Detect unclosed placeholders: { not followed by } on the same line
    for line in template.splitlines():
        opens = line.count("{")
        closes = line.count("}")
        if opens != closes:
            return f"Template contains unbalanced braces on line: {line!r}"
    return None


def _render_template(template: str, fields: dict) -> str:
    """Render template with fields; silently drop lines whose placeholders are
    all empty. Mirrors the Python renderer in services/delivery_message_renderer.py."""
    output = []
    for raw_line in template.splitlines():
        ph_names = _PLACEHOLDER_RE.findall(raw_line)
        if ph_names and all(not fields.get(ph, "") for ph in ph_names):
            continue  # drop line
        try:
            rendered = raw_line.format_map(
                {k: fields.get(k, "") for k in ph_names}
            )
        except Exception:
            rendered = raw_line
        output.append(rendered)

    # Collapse 3+ blank lines to 2
    collapsed, blank_run = [], 0
    for line in output:
        if not line.strip():
            blank_run += 1
            if blank_run <= 2:
                collapsed.append(line)
        else:
            blank_run = 0
            collapsed.append(line)

    # Strip leading/trailing blank lines
    while collapsed and not collapsed[0].strip():
        collapsed.pop(0)
    while collapsed and not collapsed[-1].strip():
        collapsed.pop()

    return "\n".join(collapsed)


def _build_response(template: str, default_template: str) -> dict:
    return {
        "template":        template,
        "isDefault":       template.strip() == default_template.strip(),
        "defaultTemplate": default_template,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@admin_bp.route("/api/delivery-template", methods=["GET"])
def get_delivery_template():
    """Return the active delivery template."""
    from services.delivery_message_renderer import get_global_template, DEFAULT_TEMPLATE
    template = get_global_template()
    return jsonify(_build_response(template, DEFAULT_TEMPLATE)), 200


@admin_bp.route("/api/delivery-template", methods=["PUT"])
def update_delivery_template():
    """Save a custom delivery template."""
    from services.delivery_message_renderer import (
        set_global_template, DEFAULT_TEMPLATE,
    )
    data = request.get_json(silent=True) or {}
    template = data.get("template", "")

    error = _validate_template(template)
    if error:
        return jsonify({"error": error}), 400

    try:
        set_global_template(template)
    except Exception as exc:
        logger.exception("Failed to save delivery template")
        return jsonify({"error": str(exc)}), 500

    return jsonify(_build_response(template, DEFAULT_TEMPLATE)), 200


@admin_bp.route("/api/delivery-template", methods=["DELETE"])
def restore_default_template():
    """Restore the built-in default template."""
    from services.delivery_message_renderer import (
        set_global_template, DEFAULT_TEMPLATE,
    )
    try:
        set_global_template(None)   # NULL → renderer falls back to DEFAULT_TEMPLATE
    except Exception as exc:
        logger.exception("Failed to restore default template")
        return jsonify({"error": str(exc)}), 500

    return jsonify(_build_response(DEFAULT_TEMPLATE, DEFAULT_TEMPLATE)), 200


@admin_bp.route("/api/delivery-template/preview", methods=["POST"])
def preview_delivery_template():
    """Render a template with sample data and return the preview string."""
    from services.delivery_message_renderer import (
        get_global_template, DEFAULT_TEMPLATE, render_template as _render,
    )
    data = request.get_json(silent=True) or {}
    template = data.get("template", "")

    if not template or not template.strip():
        template = get_global_template()

    error = _validate_template(template)
    if error:
        return jsonify({"error": error}), 400

    try:
        preview = _render(template, dict(_SAMPLE_VALUES))
    except Exception as exc:
        logger.exception("Failed to render preview")
        return jsonify({"error": str(exc)}), 500

    return jsonify({"preview": preview}), 200


# ── Static file serving (React admin panel) ───────────────────────────────────

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@admin_bp.route("/", defaults={"path": ""})
@admin_bp.route("/<path:path>")
def serve_admin_panel(path: str):
    """Serve the React admin panel static files.

    All unknown paths fall back to index.html so the React router handles them.
    """
    if path and os.path.exists(os.path.join(_STATIC_DIR, path)):
        return send_from_directory(_STATIC_DIR, path)
    return send_from_directory(_STATIC_DIR, "index.html")


# ── Registration helper ───────────────────────────────────────────────────────

def register_admin_web_routes(app: Flask) -> None:
    """Register the admin web blueprint on *app*.

    Call this once from the entry-point after creating the Flask app::

        from admin_web.routes import register_admin_web_routes
        register_admin_web_routes(app)
    """
    app.register_blueprint(admin_bp)
    logger.info("Admin web routes registered at /admin/")

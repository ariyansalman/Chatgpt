"""V42 — Plugin & Module Manager service.

Manages the internal module registry stored in ``module_configs``.
Only built-in modules are managed — no external plugin installation.

Status values: 'enabled' | 'maintenance' | 'disabled'
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from database import get_db_session
from database.models import ModuleConfig

logger = logging.getLogger(__name__)

# ─── Built-in module registry ────────────────────────────────────────────────
# Each entry is the canonical default for a built-in module.
# slug must be unique and stable — it is the primary key in bot logic.
BUILTIN_MODULES: list[dict] = [
    {
        "slug":         "wallet",
        "name":         "Wallet",
        "version":      "4.1.0",
        "description":  "Multi-currency user wallet with deposit, withdrawal, and balance management.",
        "author":       "Core",
        "dependencies": [],
        "is_core":      True,
        "category":     "payments",
    },
    {
        "slug":         "orders",
        "name":         "Orders",
        "version":      "4.1.0",
        "description":  "Order placement, status tracking, and lifecycle management.",
        "author":       "Core",
        "dependencies": ["products", "payments"],
        "is_core":      True,
        "category":     "commerce",
    },
    {
        "slug":         "products",
        "name":         "Products",
        "version":      "4.1.0",
        "description":  "Product catalog, variants, inventory, and media.",
        "author":       "Core",
        "dependencies": ["categories"],
        "is_core":      True,
        "category":     "commerce",
    },
    {
        "slug":         "categories",
        "name":         "Categories",
        "version":      "4.1.0",
        "description":  "Product categorization hierarchy.",
        "author":       "Core",
        "dependencies": [],
        "is_core":      True,
        "category":     "commerce",
    },
    {
        "slug":         "payments",
        "name":         "Payments",
        "version":      "4.1.0",
        "description":  "Payment gateways, manual methods, and transaction processing.",
        "author":       "Core",
        "dependencies": [],
        "is_core":      True,
        "category":     "payments",
    },
    {
        "slug":         "broadcast",
        "name":         "Broadcast",
        "version":      "4.1.0",
        "description":  "Admin broadcast messaging to users, with scheduling and segmentation.",
        "author":       "Core",
        "dependencies": [],
        "is_core":      False,
        "category":     "marketing",
    },
    {
        "slug":         "coupons",
        "name":         "Coupons",
        "version":      "4.1.0",
        "description":  "Discount coupons and promotional codes with advanced rules.",
        "author":       "Core",
        "dependencies": ["orders"],
        "is_core":      False,
        "category":     "marketing",
    },
    {
        "slug":         "referral",
        "name":         "Referral",
        "version":      "4.1.0",
        "description":  "Referral links, commissions, and rewards for user referrals.",
        "author":       "Core",
        "dependencies": ["wallet"],
        "is_core":      False,
        "category":     "marketing",
    },
    {
        "slug":         "support",
        "name":         "Support",
        "version":      "4.1.0",
        "description":  "Customer support ticket system with admin replies.",
        "author":       "Core",
        "dependencies": [],
        "is_core":      False,
        "category":     "support",
    },
    {
        "slug":         "downloads",
        "name":         "Downloads",
        "version":      "4.1.0",
        "description":  "Downloadable file delivery, license key tracking.",
        "author":       "Core",
        "dependencies": ["orders", "products"],
        "is_core":      False,
        "category":     "delivery",
    },
    {
        "slug":         "delivery",
        "name":         "Delivery",
        "version":      "4.1.0",
        "description":  "Digital product delivery management and re-delivery.",
        "author":       "Core",
        "dependencies": ["orders"],
        "is_core":      False,
        "category":     "delivery",
    },
    {
        "slug":         "flash_sale",
        "name":         "Flash Sale",
        "version":      "4.1.0",
        "description":  "Time-limited flash sales with automatic start/end and broadcasts.",
        "author":       "Core",
        "dependencies": ["products"],
        "is_core":      False,
        "category":     "marketing",
    },
    {
        "slug":         "vip_system",
        "name":         "VIP System",
        "version":      "4.1.0",
        "description":  "VIP tiers, loyalty points, cashback, and reward redemption.",
        "author":       "Core",
        "dependencies": ["wallet"],
        "is_core":      False,
        "category":     "loyalty",
    },
    {
        "slug":         "fraud_detection",
        "name":         "Fraud Detection",
        "version":      "4.1.0",
        "description":  "Automated fraud scoring, velocity checks, and blacklist management.",
        "author":       "Core",
        "dependencies": ["orders", "payments"],
        "is_core":      False,
        "category":     "security",
    },
    {
        "slug":         "subscription_reminder",
        "name":         "Subscription Reminder",
        "version":      "4.1.0",
        "description":  "Automated renewal reminders for subscription products.",
        "author":       "Core",
        "dependencies": ["orders"],
        "is_core":      False,
        "category":     "automation",
    },
    {
        "slug":         "reports",
        "name":         "Reports",
        "version":      "4.1.0",
        "description":  "Business reports, export, and scheduled delivery.",
        "author":       "Core",
        "dependencies": [],
        "is_core":      False,
        "category":     "analytics",
    },
    {
        "slug":         "analytics",
        "name":         "Analytics",
        "version":      "4.1.0",
        "description":  "Sales forecasting, business insights, and daily snapshots.",
        "author":       "Core",
        "dependencies": ["orders"],
        "is_core":      False,
        "category":     "analytics",
    },
    {
        "slug":         "search",
        "name":         "Search",
        "version":      "4.1.0",
        "description":  "Full-text product search with filters.",
        "author":       "Core",
        "dependencies": ["products"],
        "is_core":      False,
        "category":     "ux",
    },
    {
        "slug":         "settings",
        "name":         "Settings",
        "version":      "4.1.0",
        "description":  "Bot configuration, feature toggles, and admin settings.",
        "author":       "Core",
        "dependencies": [],
        "is_core":      True,
        "category":     "system",
    },
    {
        "slug":         "plugin_module_manager",
        "name":         "Plugin & Module Manager",
        "version":      "4.2.0",
        "description":  "Centralized management of all built-in modules and their status.",
        "author":       "Core",
        "dependencies": ["settings"],
        "is_core":      True,
        "category":     "system",
    },
    {
        "slug":         "global_activity_timeline",
        "name":         "Global Activity Timeline",
        "version":      "4.2.0",
        "description":  "Centralized audit trail of every system action, with search, filter, and export.",
        "author":       "Core",
        "dependencies": [],
        "is_core":      True,
        "category":     "system",
    },
    {
        "slug":         "anti_spam",
        "name":         "Anti-Spam",
        "version":      "4.1.0",
        "description":  "Rate limiting, spam scoring, and automatic moderation actions.",
        "author":       "Core",
        "dependencies": [],
        "is_core":      False,
        "category":     "security",
    },
    {
        "slug":         "order_history_ui",
        "name":         "Order History & Order Details",
        "version":      "4.2.0",
        "description":  "Modern paginated order history with rich summaries, "
                        "detailed order view with delivery info, auto-detected "
                        "content fields, copy buttons, password masking, "
                        "Buy Again shortcut, and admin settings.",
        "author":       "Core",
        "dependencies": ["orders"],
        "is_core":      False,
        "category":     "ux",
    },
    {
        "slug":         "product_pagination",
        "name":         "Product Pagination",
        "version":      "4.2.0",
        "description":  "Paginated product browser with configurable page size, "
                        "navigation controls, stock display, and admin settings.",
        "author":       "Core",
        "dependencies": ["products"],
        "is_core":      False,
        "category":     "ux",
    },
    {
        "slug":         "api_manager",
        "name":         "API & Integration Manager",
        "version":      "4.1.0",
        "description":  "Centralised API/integration registry with health monitoring.",
        "author":       "Core",
        "dependencies": [],
        "is_core":      False,
        "category":     "integrations",
    },
]


# ─── Status helpers ───────────────────────────────────────────────────────────

STATUS_EMOJI = {
    "enabled":     "🟢",
    "maintenance": "🟡",
    "disabled":    "🔴",
}

STATUS_LABEL = {
    "enabled":     "Enabled",
    "maintenance": "Maintenance",
    "disabled":    "Disabled",
}

VALID_STATUSES = {"enabled", "maintenance", "disabled"}


# ─── Seed / upsert ───────────────────────────────────────────────────────────

def seed_modules() -> None:
    """Ensure every built-in module has a row in module_configs.
    Existing rows are preserved (status is never reset by a seed).
    """
    try:
        with get_db_session() as s:
            for m in BUILTIN_MODULES:
                existing = s.query(ModuleConfig).filter_by(slug=m["slug"]).first()
                if existing is None:
                    row = ModuleConfig(
                        slug=m["slug"],
                        name=m["name"],
                        version=m["version"],
                        description=m["description"],
                        author=m["author"],
                        dependencies=json.dumps(m["dependencies"]),
                        is_core=m["is_core"],
                        category=m.get("category", "misc"),
                        status="enabled",
                    )
                    s.add(row)
            s.commit()
        logger.info("module_manager: seed complete")
    except Exception:
        logger.exception("module_manager: seed_modules failed")


# ─── CRUD ────────────────────────────────────────────────────────────────────

def get_all_modules() -> list[ModuleConfig]:
    """Return all module rows ordered by category, name."""
    try:
        with get_db_session() as s:
            rows = (
                s.query(ModuleConfig)
                .order_by(ModuleConfig.category, ModuleConfig.name)
                .all()
            )
            s.expunge_all()
            return rows
    except Exception:
        logger.exception("module_manager: get_all_modules failed")
        return []


def get_module(slug: str) -> Optional[ModuleConfig]:
    """Return a single module by slug, or None."""
    try:
        with get_db_session() as s:
            row = s.query(ModuleConfig).filter_by(slug=slug).first()
            if row:
                s.expunge(row)
            return row
    except Exception:
        logger.exception("module_manager: get_module(%s) failed", slug)
        return None


def set_module_status(slug: str, status: str) -> tuple[bool, str]:
    """Change a module's status.

    Returns (success, message).
    Refuses to disable a core module or a module that others depend on.
    """
    if status not in VALID_STATUSES:
        return False, f"Invalid status '{status}'."

    try:
        with get_db_session() as s:
            row = s.query(ModuleConfig).filter_by(slug=slug).first()
            if row is None:
                return False, f"Module '{slug}' not found."

            if status == "disabled":
                # Safety: core modules cannot be disabled
                if row.is_core:
                    return False, f"Cannot disable core module '{row.name}'."
                # Safety: check if any enabled module depends on this one
                dependents = _find_dependents(s, slug)
                if dependents:
                    names = ", ".join(dependents)
                    return False, f"Cannot disable '{row.name}' — required by: {names}."

            row.status = status
            row.last_updated_at = datetime.utcnow()
            s.commit()
        return True, f"Module '{slug}' status set to '{status}'."
    except Exception as exc:
        logger.exception("module_manager: set_module_status(%s, %s) failed", slug, status)
        return False, str(exc)


def _find_dependents(session, slug: str) -> list[str]:
    """Return names of enabled/maintenance modules that list slug in their dependencies."""
    result = []
    try:
        rows = session.query(ModuleConfig).filter(
            ModuleConfig.status != "disabled"
        ).all()
        for row in rows:
            if row.slug == slug:
                continue
            try:
                deps = json.loads(row.dependencies or "[]")
            except Exception:
                deps = []
            if slug in deps:
                result.append(row.name)
    except Exception:
        pass
    return result


def check_dependencies(slug: str) -> dict:
    """Check dependency status for a given module.

    Returns dict with keys: ok (bool), missing (list), disabled (list), circular (list).
    """
    result = {"ok": True, "missing": [], "disabled": [], "circular": []}
    try:
        mod = get_module(slug)
        if mod is None:
            result["ok"] = False
            return result
        try:
            deps = json.loads(mod.dependencies or "[]")
        except Exception:
            deps = []

        with get_db_session() as s:
            for dep in deps:
                dep_row = s.query(ModuleConfig).filter_by(slug=dep).first()
                if dep_row is None:
                    result["missing"].append(dep)
                    result["ok"] = False
                elif dep_row.status == "disabled":
                    result["disabled"].append(dep_row.name)
                    result["ok"] = False

        # Simple circular check: if this module appears in its own dependency tree
        if _is_circular(slug, deps):
            result["circular"].append(slug)
            result["ok"] = False

    except Exception:
        logger.exception("module_manager: check_dependencies(%s) failed", slug)
        result["ok"] = False
    return result


def _is_circular(slug: str, deps: list[str], visited: Optional[set] = None) -> bool:
    if visited is None:
        visited = set()
    if slug in visited:
        return True
    visited = visited | {slug}
    for dep in deps:
        dep_mod = get_module(dep)
        if dep_mod is None:
            continue
        try:
            sub_deps = json.loads(dep_mod.dependencies or "[]")
        except Exception:
            sub_deps = []
        if _is_circular(dep, sub_deps, visited):
            return True
    return False


def get_module_stats() -> dict:
    """Return summary counts."""
    stats = {"enabled": 0, "maintenance": 0, "disabled": 0, "total": 0, "core": 0}
    try:
        with get_db_session() as s:
            rows = s.query(ModuleConfig).all()
            stats["total"] = len(rows)
            for r in rows:
                stats[r.status] = stats.get(r.status, 0) + 1
                if r.is_core:
                    stats["core"] += 1
    except Exception:
        pass
    return stats

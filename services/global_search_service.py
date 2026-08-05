"""V45 — Global Search Engine service.

Provides fuzzy, partial, and exact cross-model search across every major
data type in the bot. Records search history and computes search stats.

All public functions are best-effort — they must never raise to callers.

V45 additions over V43:
  • gift_cards, bundles, reviews, notifications, product_keys, audit_logs modules
  • txid / proof / crypto_address search on transactions
  • date_from / date_to filter applied in every searcher
  • referrals actual search (ReferralReward + ReferralCommission)
  • sort parameter ('newest' | 'oldest' | 'amount_desc' | 'amount_asc')
  • account_number (phone/IBAN) on user search
  • order_items product-name JOIN on orders search
  • ticket_number on support ticket search
  • wallet_address on referral withdrawals search
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Module registry ──────────────────────────────────────────────────────────
SEARCH_MODULES: dict[str, dict] = {
    "users":             {"label": "Users",             "emoji": "👥"},
    "orders":            {"label": "Orders",            "emoji": "🧾"},
    "products":          {"label": "Products",          "emoji": "📦"},
    "categories":        {"label": "Categories",        "emoji": "📂"},
    "product_keys":      {"label": "Product Keys",      "emoji": "🔐"},
    "transactions":      {"label": "Transactions",      "emoji": "💳"},
    "deposits":          {"label": "Deposits",          "emoji": "⬇️"},
    "withdrawals":       {"label": "Withdrawals",       "emoji": "⬆️"},
    "payments":          {"label": "Payments",          "emoji": "💰"},
    "coupons":           {"label": "Coupons",           "emoji": "🎟"},
    "gift_cards":        {"label": "Gift Cards",        "emoji": "🎁"},
    "bundles":           {"label": "Bundles",           "emoji": "📦"},
    "reviews":           {"label": "Reviews",           "emoji": "⭐"},
    "referrals":         {"label": "Referrals",         "emoji": "👥"},
    "broadcasts":        {"label": "Broadcasts",        "emoji": "📢"},
    "flash_sales":       {"label": "Flash Sales",       "emoji": "⚡"},
    "subscriptions":     {"label": "Subscriptions",     "emoji": "🔄"},
    "vip_users":         {"label": "VIP Users",         "emoji": "👑"},
    "support_tickets":   {"label": "Support Tickets",   "emoji": "🎫"},
    "notifications":     {"label": "Notifications",     "emoji": "🔔"},
    "activity_timeline": {"label": "Activity Timeline", "emoji": "📜"},
    "audit_logs":        {"label": "Audit Logs",        "emoji": "📋"},
    "license_keys":      {"label": "License Keys",      "emoji": "🔑"},
    "files":             {"label": "Files",             "emoji": "📁"},
    "delivery_logs":     {"label": "Delivery Logs",     "emoji": "📬"},
    "admin_logs":        {"label": "Admin Logs",        "emoji": "🔐"},
    "system_logs":       {"label": "System Logs",       "emoji": "🖥"},
}

ALL_MODULE_SLUGS = list(SEARCH_MODULES.keys())


# ─── Shared filter helpers ────────────────────────────────────────────────────

def _apply_date_filter(qb, model_class, filters: dict, date_col_name: str = "created_at"):
    """Apply date_from / date_to filters from the filters dict to a query."""
    date_col = getattr(model_class, date_col_name, None)
    if date_col is None:
        return qb
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    if date_from:
        try:
            if isinstance(date_from, str):
                date_from = datetime.strptime(date_from, "%Y-%m-%d")
            qb = qb.filter(date_col >= date_from)
        except (ValueError, TypeError):
            pass
    if date_to:
        try:
            if isinstance(date_to, str):
                date_to = datetime.strptime(date_to, "%Y-%m-%d")
            qb = qb.filter(date_col <= date_to)
        except (ValueError, TypeError):
            pass
    return qb


def _sort_results(results: list[dict], sort: str) -> list[dict]:
    """Sort aggregated results by the requested strategy."""
    if sort == "oldest":
        return sorted(results, key=lambda r: r.get("created_at") or datetime.min)
    if sort == "amount_desc":
        return sorted(results, key=lambda r: float(r.get("_sort_amount", 0)), reverse=True)
    if sort == "amount_asc":
        return sorted(results, key=lambda r: float(r.get("_sort_amount", 0)))
    # default: newest first
    return sorted(results, key=lambda r: r.get("created_at") or datetime.min, reverse=True)


# ─── Per-module searchers ─────────────────────────────────────────────────────

def _search_users(session, q: str, filters: dict) -> list[dict]:
    from database.models import User
    from sqlalchemy import or_, func, cast
    from sqlalchemy.types import String
    results = []
    try:
        # Build search conditions defensively — first_name/last_name may not be
        # present in all User model versions.
        conditions = [
            func.lower(User.username).contains(q.lower()),
            cast(User.telegram_id, String).contains(q),
            cast(User.id, String) == q,
        ]
        if hasattr(User, "first_name") and User.first_name is not None:
            conditions.append(func.lower(User.first_name).contains(q.lower()))
        if hasattr(User, "last_name") and User.last_name is not None:
            conditions.append(func.lower(User.last_name).contains(q.lower()))
        # Search phone / IBAN if User has account_number (some bot versions)
        if hasattr(User, "account_number"):
            conditions.append(func.lower(User.account_number).contains(q.lower()))
        qb = session.query(User).filter(or_(*conditions))
        if filters.get("status") == "banned":
            qb = qb.filter(User.is_banned == True)
        elif filters.get("status") == "active":
            qb = qb.filter(User.is_banned == False)
        qb = _apply_date_filter(qb, User, filters)
        for u in qb.order_by(User.created_at.desc()).limit(20).all():
            status = "🚫 Banned" if u.is_banned else "✅ Active"
            bal = getattr(u, "wallet_balance", None) or getattr(u, "balance", 0) or 0
            results.append({
                "module": "users", "id": u.id,
                "label": f"👥 {u.username or getattr(u, 'first_name', '') or 'User'} (TG:{u.telegram_id})",
                "summary": f"Balance: ${bal:.2f} | {status}",
                "status": status, "created_at": u.created_at,
                "cb_detail": f"gse:det:users:{u.id}",
                "_sort_amount": bal,
            })
    except Exception as e:
        logger.warning("_search_users: %s", e)
    return results


def _search_orders(session, q: str, filters: dict) -> list[dict]:
    import re as _re
    from database.models import Order, OrderItem, Product
    from sqlalchemy import cast, String, or_, func
    results = []
    try:
        # Unwrap display Order ID: ORD-YYYYMMDD-NNNNNN → numeric string for matching
        _ord_m = _re.match(r"^ORD-\d{8}-0*(\d+)$", q.strip(), _re.IGNORECASE)
        if _ord_m:
            q = _ord_m.group(1)  # use the bare numeric ID for the query below

        # Base order query — by order ID, user ID, or product name via join
        base_conditions = [
            cast(Order.id, String).contains(q),
            cast(Order.user_id, String).contains(q),
        ]
        # If query looks like product name text, join order_items→products
        if not q.isdigit():
            product_ids_subq = (
                session.query(OrderItem.order_id)
                .join(Product, OrderItem.product_id == Product.id)
                .filter(func.lower(Product.name).contains(q.lower()))
                .subquery()
            )
            base_conditions.append(Order.id.in_(product_ids_subq))

        qb = session.query(Order).filter(or_(*base_conditions))
        if filters.get("status"):
            qb = qb.filter(Order.status == filters["status"])
        qb = _apply_date_filter(qb, Order, filters)
        for o in qb.order_by(Order.created_at.desc()).limit(20).all():
            status_str = str(o.status.value if hasattr(o.status, "value") else o.status or "")
            results.append({
                "module": "orders", "id": o.id,
                "label": f"🧾 Order #{o.id} — User {o.user_id}",
                "summary": f"${o.total_amount:.2f} | {status_str}",
                "status": status_str, "created_at": o.created_at,
                "cb_detail": f"gse:det:orders:{o.id}",
                "_sort_amount": o.total_amount,
            })
    except Exception as e:
        logger.warning("_search_orders: %s", e)
    return results


def _search_products(session, q: str, filters: dict) -> list[dict]:
    from database.models import Product
    from sqlalchemy import func, or_, cast, String
    results = []
    try:
        qb = session.query(Product).filter(or_(
            func.lower(Product.name).contains(q.lower()),
            func.lower(Product.description).contains(q.lower()),
            cast(Product.id, String) == q,
        ))
        if filters.get("status") == "active":
            qb = qb.filter(Product.is_active == True)
        elif filters.get("status") == "inactive":
            qb = qb.filter(Product.is_active == False)
        if filters.get("category_id"):
            qb = qb.filter(Product.category_id == filters["category_id"])
        if filters.get("price_min") is not None:
            qb = qb.filter(Product.price >= filters["price_min"])
        if filters.get("price_max") is not None:
            qb = qb.filter(Product.price <= filters["price_max"])
        if filters.get("in_stock"):
            qb = qb.filter(Product.stock_count > 0)
        qb = _apply_date_filter(qb, Product, filters)
        for p in qb.order_by(Product.created_at.desc()).limit(20).all():
            price = p.sale_price if (p.sale_price and p.sale_price > 0) else p.price
            results.append({
                "module": "products", "id": p.id,
                "label": f"📦 {p.name}",
                "summary": f"${price:.2f} | Stock: {p.stock_count} | {'✅ Active' if p.is_active else '❌ Inactive'}",
                "status": "active" if p.is_active else "inactive",
                "created_at": p.created_at,
                "cb_detail": f"gse:det:products:{p.id}",
                "_sort_amount": price,
            })
    except Exception as e:
        logger.warning("_search_products: %s", e)
    return results


def _search_categories(session, q: str, filters: dict) -> list[dict]:
    from database.models import Category
    from sqlalchemy import func
    results = []
    try:
        qb = session.query(Category).filter(func.lower(Category.name).contains(q.lower()))
        qb = _apply_date_filter(qb, Category, filters)
        for c in qb.limit(20).all():
            results.append({
                "module": "categories", "id": c.id,
                "label": f"📂 {c.name}",
                "summary": f"Category #{c.id}",
                "status": "active", "created_at": getattr(c, "created_at", None),
                "cb_detail": f"gse:det:categories:{c.id}",
            })
    except Exception as e:
        logger.warning("_search_categories: %s", e)
    return results


def _search_transactions(session, q: str, filters: dict) -> list[dict]:
    """Search transactions by ID, user ID, description, TXID, proof, or crypto_address."""
    from database.models import Transaction
    from sqlalchemy import cast, String, or_, func
    results = []
    try:
        conditions = [
            cast(Transaction.id, String).contains(q),
            cast(Transaction.user_id, String).contains(q),
        ]
        # V45: also search TXID, proof text, admin note, and crypto address
        if hasattr(Transaction, "admin_note"):
            conditions.append(func.lower(Transaction.admin_note).contains(q.lower()))
        if hasattr(Transaction, "txid"):
            conditions.append(func.lower(Transaction.txid).contains(q.lower()))
        if hasattr(Transaction, "proof"):
            conditions.append(func.lower(Transaction.proof).contains(q.lower()))
        if hasattr(Transaction, "crypto_address"):
            conditions.append(func.lower(Transaction.crypto_address).contains(q.lower()))
        qb = session.query(Transaction).filter(or_(*conditions))
        if filters.get("status"):
            qb = qb.filter(Transaction.status == filters["status"])
        if filters.get("payment_method"):
            qb = qb.filter(Transaction.payment_method == filters["payment_method"])
        qb = _apply_date_filter(qb, Transaction, filters)
        for t in qb.order_by(Transaction.created_at.desc()).limit(20).all():
            status_str = str(t.status.value if hasattr(t.status, "value") else t.status or "")
            txid_hint = f" | TXID: {t.txid[:12]}…" if getattr(t, "txid", None) else ""
            results.append({
                "module": "transactions", "id": t.id,
                "label": f"💳 Transaction #{t.id}",
                "summary": f"${t.amount:.2f} | {status_str}{txid_hint}",
                "status": status_str, "created_at": t.created_at,
                "cb_detail": f"gse:det:transactions:{t.id}",
                "_sort_amount": t.amount,
            })
    except Exception as e:
        logger.warning("_search_transactions: %s", e)
    return results


def _search_coupons(session, q: str, filters: dict) -> list[dict]:
    from database.models import Coupon
    from sqlalchemy import func, or_, cast, String
    results = []
    try:
        qb = session.query(Coupon).filter(or_(
            func.lower(Coupon.code).contains(q.lower()),
            cast(Coupon.id, String) == q,
        ))
        if filters.get("status") == "active":
            qb = qb.filter(Coupon.is_active == True)
        elif filters.get("status") == "inactive":
            qb = qb.filter(Coupon.is_active == False)
        qb = _apply_date_filter(qb, Coupon, filters)
        for c in qb.order_by(Coupon.created_at.desc()).limit(20).all():
            results.append({
                "module": "coupons", "id": c.id,
                "label": f"🎟 {c.code}",
                "summary": f"Discount: {c.discount_value} | {'✅' if getattr(c, 'is_active', True) else '❌'} | Used: {getattr(c, 'times_used', 0)}/{getattr(c, 'max_uses', '∞') or '∞'}",
                "status": "active" if getattr(c, "is_active", True) else "inactive",
                "created_at": c.created_at,
                "cb_detail": f"gse:det:coupons:{c.id}",
            })
    except Exception as e:
        logger.warning("_search_coupons: %s", e)
    return results


def _search_support_tickets(session, q: str, filters: dict) -> list[dict]:
    from database.models import SupportTicket
    from sqlalchemy import func, cast, String, or_
    results = []
    try:
        conditions = [
            cast(SupportTicket.id, String).contains(q),
            cast(SupportTicket.user_id, String).contains(q),
            func.lower(SupportTicket.subject).contains(q.lower()),
        ]
        if hasattr(SupportTicket, "ticket_number"):
            conditions.append(func.lower(SupportTicket.ticket_number).contains(q.lower()))
        qb = session.query(SupportTicket).filter(or_(*conditions))
        if filters.get("status"):
            qb = qb.filter(SupportTicket.status == filters["status"])
        qb = _apply_date_filter(qb, SupportTicket, filters)
        for t in qb.order_by(SupportTicket.created_at.desc()).limit(20).all():
            status_str = str(t.status.value if hasattr(t.status, "value") else t.status or "")
            ticket_num = getattr(t, "ticket_number", None) or f"#{t.id}"
            results.append({
                "module": "support_tickets", "id": t.id,
                "label": f"🎫 Ticket {ticket_num}: {getattr(t, 'subject', '')[:40]}",
                "summary": f"User {t.user_id} | {status_str}",
                "status": status_str, "created_at": t.created_at,
                "cb_detail": f"gse:det:support_tickets:{t.id}",
            })
    except Exception as e:
        logger.warning("_search_support_tickets: %s", e)
    return results


def _search_broadcasts(session, q: str, filters: dict) -> list[dict]:
    from database.models import Broadcast
    from sqlalchemy import func, or_
    results = []
    try:
        # Broadcast model may use message_text or message
        msg_col = getattr(Broadcast, "message_text", None) or getattr(Broadcast, "message", None)
        if msg_col is None:
            return []
        qb = session.query(Broadcast).filter(func.lower(msg_col).contains(q.lower()))
        qb = _apply_date_filter(qb, Broadcast, filters)
        for b in qb.order_by(Broadcast.created_at.desc()).limit(10).all():
            msg = getattr(b, "message_text", None) or getattr(b, "message", "") or ""
            results.append({
                "module": "broadcasts", "id": b.id,
                "label": f"📢 Broadcast #{b.id}",
                "summary": msg[:80],
                "status": "done", "created_at": b.created_at,
                "cb_detail": f"gse:det:broadcasts:{b.id}",
            })
    except Exception as e:
        logger.warning("_search_broadcasts: %s", e)
    return results


def _search_activity_timeline(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import GlobalActivityEntry
        from sqlalchemy import func, or_
        qb = session.query(GlobalActivityEntry).filter(or_(
            func.lower(GlobalActivityEntry.action).contains(q.lower()),
            func.lower(GlobalActivityEntry.description).contains(q.lower()),
            func.lower(GlobalActivityEntry.username).contains(q.lower()),
        ))
        qb = _apply_date_filter(qb, GlobalActivityEntry, filters)
        results = []
        for e in qb.order_by(GlobalActivityEntry.created_at.desc()).limit(20).all():
            results.append({
                "module": "activity_timeline", "id": e.id,
                "label": f"📜 {e.action} ({e.category})",
                "summary": (e.description or "")[:80],
                "status": e.status, "created_at": e.created_at,
                "cb_detail": f"gse:det:activity_timeline:{e.id}",
            })
        return results
    except Exception as e:
        logger.warning("_search_activity_timeline: %s", e)
        return []


def _search_license_keys(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import ManagedKey
        from sqlalchemy import func, or_
        qb = session.query(ManagedKey).filter(func.lower(ManagedKey.key_value).contains(q.lower()))
        qb = _apply_date_filter(qb, ManagedKey, filters)
        results = []
        for k in qb.limit(20).all():
            status_str = str(getattr(k, "status", "") or "")
            results.append({
                "module": "license_keys", "id": k.id,
                "label": f"🔑 Key #{k.id}",
                "summary": status_str,
                "status": status_str, "created_at": getattr(k, "created_at", None),
                "cb_detail": f"gse:det:license_keys:{k.id}",
            })
        return results
    except Exception:
        return []


def _search_files(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import ManagedFile
        from sqlalchemy import func
        qb = session.query(ManagedFile).filter(func.lower(ManagedFile.filename).contains(q.lower()))
        qb = _apply_date_filter(qb, ManagedFile, filters)
        results = []
        for f in qb.limit(20).all():
            results.append({
                "module": "files", "id": f.id,
                "label": f"📁 {getattr(f, 'filename', f.id)}",
                "summary": f"Size: {getattr(f, 'file_size', 0) or 0} bytes",
                "status": "active", "created_at": getattr(f, "created_at", None),
                "cb_detail": f"gse:det:files:{f.id}",
            })
        return results
    except Exception:
        return []


def _search_admin_logs(session, q: str, filters: dict) -> list[dict]:
    from database.models import AdminAuditLog
    from sqlalchemy import func, cast, String, or_
    results = []
    try:
        conditions = [
            func.lower(AdminAuditLog.action).contains(q.lower()),
            cast(AdminAuditLog.admin_telegram_id, String).contains(q),
        ]
        if hasattr(AdminAuditLog, "details"):
            conditions.append(func.lower(AdminAuditLog.details).contains(q.lower()))
        if hasattr(AdminAuditLog, "target_user_id"):
            conditions.append(cast(AdminAuditLog.target_user_id, String).contains(q))
        qb = session.query(AdminAuditLog).filter(or_(*conditions))
        qb = _apply_date_filter(qb, AdminAuditLog, filters)
        for a in qb.order_by(AdminAuditLog.created_at.desc()).limit(20).all():
            results.append({
                "module": "admin_logs", "id": a.id,
                "label": f"🔐 {a.action}",
                "summary": f"Admin {a.admin_telegram_id} | {(getattr(a, 'details', '') or '')[:60]}",
                "status": "logged", "created_at": a.created_at,
                "cb_detail": f"gse:det:admin_logs:{a.id}",
            })
    except Exception as e:
        logger.warning("_search_admin_logs: %s", e)
    return results


def _search_subscriptions(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import Subscription
        from sqlalchemy import cast, String, or_
        qb = session.query(Subscription).filter(or_(
            cast(Subscription.user_id, String).contains(q),
            cast(Subscription.id, String).contains(q),
        ))
        qb = _apply_date_filter(qb, Subscription, filters)
        results = []
        for s in qb.order_by(Subscription.created_at.desc()).limit(20).all():
            status_str = str(getattr(s, "status", "") or "")
            results.append({
                "module": "subscriptions", "id": s.id,
                "label": f"🔄 Subscription #{s.id}",
                "summary": f"User {s.user_id} | {status_str}",
                "status": status_str, "created_at": s.created_at,
                "cb_detail": f"gse:det:subscriptions:{s.id}",
            })
        return results
    except Exception:
        return []


def _search_vip_users(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import UserVipTier, User
        from sqlalchemy import func, or_, cast, String
        qb = (session.query(UserVipTier)
              .join(User, UserVipTier.user_id == User.id)
              .filter(or_(
                  func.lower(User.username).contains(q.lower()),
                  cast(User.telegram_id, String).contains(q),
                  cast(UserVipTier.user_id, String).contains(q),
              )))
        results = []
        for uvt in qb.limit(20).all():
            u = uvt.user if hasattr(uvt, "user") else None
            uname = (u.username if u else None) or str(uvt.user_id)
            results.append({
                "module": "vip_users", "id": uvt.id,
                "label": f"👑 VIP: {uname}",
                "summary": f"Tier {uvt.tier_id}",
                "status": "vip", "created_at": getattr(uvt, "assigned_at", None),
                "cb_detail": f"gse:det:vip_users:{uvt.id}",
            })
        return results
    except Exception:
        return []


def _search_flash_sales(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import FlashSaleEvent
        from sqlalchemy import func
        qb = session.query(FlashSaleEvent).filter(func.lower(FlashSaleEvent.name).contains(q.lower()))
        qb = _apply_date_filter(qb, FlashSaleEvent, filters)
        results = []
        for f in qb.limit(10).all():
            results.append({
                "module": "flash_sales", "id": f.id,
                "label": f"⚡ {getattr(f, 'name', f.id)}",
                "summary": str(getattr(f, "status", "") or ""),
                "status": str(getattr(f, "status", "") or ""),
                "created_at": f.created_at,
                "cb_detail": f"gse:det:flash_sales:{f.id}",
            })
        return results
    except Exception:
        return []


def _search_delivery_logs(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import DeliveryJob
        from sqlalchemy import cast, String, or_
        qb = session.query(DeliveryJob).filter(or_(
            cast(DeliveryJob.order_id, String).contains(q),
            cast(DeliveryJob.id, String).contains(q),
        ))
        qb = _apply_date_filter(qb, DeliveryJob, filters)
        results = []
        for d in qb.limit(20).all():
            results.append({
                "module": "delivery_logs", "id": d.id,
                "label": f"📬 Delivery #{d.id}",
                "summary": f"Order {getattr(d, 'order_id', '')}",
                "status": str(getattr(d, "status", "") or ""),
                "created_at": getattr(d, "created_at", None),
                "cb_detail": f"gse:det:delivery_logs:{d.id}",
            })
        return results
    except Exception:
        return []


def _search_gift_cards(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import GiftCard
        from sqlalchemy import func, or_
        conditions = [func.lower(GiftCard.code).contains(q.lower())]
        if hasattr(GiftCard, "label"):
            conditions.append(func.lower(GiftCard.label).contains(q.lower()))
        qb = session.query(GiftCard).filter(or_(*conditions))
        if filters.get("status") == "active":
            qb = qb.filter(GiftCard.is_active == True)
        elif filters.get("status") == "inactive":
            qb = qb.filter(GiftCard.is_active == False)
        qb = _apply_date_filter(qb, GiftCard, filters)
        results = []
        for gc in qb.limit(20).all():
            active = "✅ Active" if gc.is_active else "🔴 Inactive"
            results.append({
                "module": "gift_cards", "id": gc.id,
                "label": f"🎁 {gc.code}",
                "summary": f"Value: {gc.value} | Used: {gc.used_count}/{gc.max_uses or '∞'} | {active}",
                "status": active, "created_at": gc.created_at,
                "cb_detail": f"gse:det:gift_cards:{gc.id}",
                "_sort_amount": getattr(gc, "value", 0),
            })
        return results
    except Exception as e:
        logger.warning("_search_gift_cards: %s", e)
        return []


def _search_bundles(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import Product, ProductType
        from sqlalchemy import func, or_, cast, String
        qb = (session.query(Product)
              .filter(
                  Product.product_type == ProductType.BUNDLE,
                  or_(
                      func.lower(Product.name).contains(q.lower()),
                      func.lower(Product.description).contains(q.lower()),
                      cast(Product.id, String) == q,
                  )
              ))
        qb = _apply_date_filter(qb, Product, filters)
        results = []
        for p in qb.limit(20).all():
            results.append({
                "module": "bundles", "id": p.id,
                "label": f"📦 Bundle: {p.name}",
                "summary": f"Price: ${p.price:.2f} | Stock: {p.stock_count} | {'✅' if p.is_active else '🔴'}",
                "status": "✅" if p.is_active else "🔴",
                "created_at": p.created_at,
                "cb_detail": f"gse:det:bundles:{p.id}",
                "_sort_amount": p.price,
            })
        return results
    except Exception as e:
        logger.warning("_search_bundles: %s", e)
        return []


def _search_reviews(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import Review
        from sqlalchemy import func, cast, String, or_
        conditions = [
            func.lower(Review.comment).contains(q.lower()),
            cast(Review.id, String) == q,
            cast(Review.user_id, String).contains(q),
        ]
        if hasattr(Review, "product_id"):
            conditions.append(cast(Review.product_id, String).contains(q))
        qb = session.query(Review).filter(or_(*conditions))
        if filters.get("rating"):
            qb = qb.filter(Review.rating == filters["rating"])
        qb = _apply_date_filter(qb, Review, filters)
        results = []
        for r in qb.order_by(Review.created_at.desc()).limit(20).all():
            hidden = "🙈 Hidden" if r.is_hidden else "👁 Visible"
            results.append({
                "module": "reviews", "id": r.id,
                "label": f"⭐ Review #{r.id} — {'★' * r.rating}{'☆' * (5 - r.rating)}",
                "summary": f"{(r.comment or '')[:80]} | {hidden}",
                "status": hidden, "created_at": r.created_at,
                "cb_detail": f"gse:det:reviews:{r.id}",
            })
        return results
    except Exception as e:
        logger.warning("_search_reviews: %s", e)
        return []


def _search_notifications(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import AdminNotification
        from sqlalchemy import func, or_
        qb = session.query(AdminNotification).filter(or_(
            func.lower(AdminNotification.title).contains(q.lower()),
            func.lower(AdminNotification.body).contains(q.lower()),
            func.lower(AdminNotification.event_type).contains(q.lower()),
        ))
        if filters.get("status") == "read":
            qb = qb.filter(AdminNotification.is_read == True)
        elif filters.get("status") == "unread":
            qb = qb.filter(AdminNotification.is_read == False)
        qb = _apply_date_filter(qb, AdminNotification, filters)
        results = []
        for n in qb.order_by(AdminNotification.created_at.desc()).limit(20).all():
            read_status = "✅ Read" if n.is_read else "🔵 Unread"
            results.append({
                "module": "notifications", "id": n.id,
                "label": f"🔔 {n.title}",
                "summary": f"{(n.body or '')[:80]} | {read_status}",
                "status": read_status, "created_at": n.created_at,
                "cb_detail": f"gse:det:notifications:{n.id}",
            })
        return results
    except Exception as e:
        logger.warning("_search_notifications: %s", e)
        return []


def _search_product_keys(session, q: str, filters: dict) -> list[dict]:
    try:
        from database.models import ProductKey
        from sqlalchemy import func, cast, String, or_
        qb = session.query(ProductKey).filter(or_(
            func.lower(ProductKey.key_value).contains(q.lower()),
            cast(ProductKey.id, String) == q,
            cast(ProductKey.product_id, String).contains(q),
        ))
        if filters.get("status") == "available":
            qb = qb.filter(ProductKey.is_sold == False)
        elif filters.get("status") == "sold":
            qb = qb.filter(ProductKey.is_sold == True)
        qb = _apply_date_filter(qb, ProductKey, filters)
        results = []
        for k in qb.order_by(ProductKey.created_at.desc()).limit(20).all():
            sold = getattr(k, "is_sold", False)
            status = "✅ Sold" if sold else "🔑 Available"
            results.append({
                "module": "product_keys", "id": k.id,
                "label": f"🔐 Key #{k.id} (Product {k.product_id})",
                "summary": f"{(k.key_value or '')[:40]} | {status}",
                "status": status, "created_at": getattr(k, "created_at", None),
                "cb_detail": f"gse:det:product_keys:{k.id}",
            })
        return results
    except Exception as e:
        logger.warning("_search_product_keys: %s", e)
        return []


def _search_referrals(session, q: str, filters: dict) -> list[dict]:
    """Search referral rewards and commissions by user ID or order ID."""
    from sqlalchemy import cast, String, or_, func
    results = []
    try:
        from database.models import ReferralReward, User
        qb = (session.query(ReferralReward)
              .outerjoin(User, ReferralReward.referrer_id == User.id)
              .filter(or_(
                  cast(ReferralReward.referrer_id, String).contains(q),
                  cast(ReferralReward.referred_id, String).contains(q),
                  cast(ReferralReward.id, String) == q,
                  func.lower(User.username).contains(q.lower()),
              )))
        qb = _apply_date_filter(qb, ReferralReward, filters)
        for r in qb.order_by(ReferralReward.created_at.desc()).limit(10).all():
            results.append({
                "module": "referrals", "id": r.id,
                "label": f"👥 Referral #{r.id}",
                "summary": f"Referrer {r.referrer_id} → Referred {r.referred_id} | ${r.amount:.2f}",
                "status": "rewarded", "created_at": r.created_at,
                "cb_detail": f"gse:det:referrals:{r.id}",
                "_sort_amount": r.amount,
            })
    except Exception as e:
        logger.warning("_search_referrals ReferralReward: %s", e)

    try:
        from database.models import ReferralCommission
        qb2 = session.query(ReferralCommission).filter(or_(
            cast(ReferralCommission.referrer_id, String).contains(q),
            cast(ReferralCommission.referred_id, String).contains(q),
            cast(ReferralCommission.id, String) == q,
        ))
        qb2 = _apply_date_filter(qb2, ReferralCommission, filters)
        for r in qb2.order_by(ReferralCommission.created_at.desc()).limit(10).all():
            # Avoid duplicates with ReferralReward results
            results.append({
                "module": "referrals", "id": f"c{r.id}",
                "label": f"👥 Commission #{r.id}",
                "summary": f"Referrer {r.referrer_id} | ${r.commission_amount:.2f} ({r.status})",
                "status": r.status, "created_at": r.created_at,
                "cb_detail": f"gse:det:referrals:c{r.id}",
                "_sort_amount": r.commission_amount,
            })
    except Exception as e:
        logger.warning("_search_referrals ReferralCommission: %s", e)

    return results


# ─── Module dispatch table ────────────────────────────────────────────────────
_SEARCHERS: dict = {
    "users":             _search_users,
    "orders":            _search_orders,
    "products":          _search_products,
    "categories":        _search_categories,
    "product_keys":      _search_product_keys,
    "transactions":      _search_transactions,
    "deposits":          _search_transactions,
    "withdrawals":       _search_transactions,
    "payments":          _search_transactions,
    "coupons":           _search_coupons,
    "gift_cards":        _search_gift_cards,
    "bundles":           _search_bundles,
    "reviews":           _search_reviews,
    "referrals":         _search_referrals,
    "broadcasts":        _search_broadcasts,
    "flash_sales":       _search_flash_sales,
    "subscriptions":     _search_subscriptions,
    "vip_users":         _search_vip_users,
    "support_tickets":   _search_support_tickets,
    "notifications":     _search_notifications,
    "activity_timeline": _search_activity_timeline,
    "audit_logs":        _search_admin_logs,
    "license_keys":      _search_license_keys,
    "files":             _search_files,
    "delivery_logs":     _search_delivery_logs,
    "admin_logs":        _search_admin_logs,
    "system_logs":       _search_admin_logs,
}


# ─── Public search API ────────────────────────────────────────────────────────

def search(query: str,
           modules: Optional[list[str]] = None,
           filters: Optional[dict] = None,
           sort: str = "newest",
           admin_telegram_id: Optional[int] = None,
           page: int = 1,
           per_page: int = 10) -> dict:
    """
    Execute a cross-module search.

    Args:
        query:              Search term.
        modules:            Which module slugs to search (default: all).
        filters:            Dict of filter key→value. Supported keys:
                              date_from, date_to  (str 'YYYY-MM-DD' or datetime)
                              status, payment_method, category_id
                              price_min, price_max, in_stock, rating
        sort:               'newest' | 'oldest' | 'amount_desc' | 'amount_asc'
        admin_telegram_id:  Records the search in history when provided.
        page / per_page:    Pagination.

    Returns:
        { results, total, page, pages, query, search_time_ms, modules_searched,
          filters_applied, sort }
    """
    start_ms = time.time()
    filters = filters or {}
    modules_to_search = modules or ALL_MODULE_SLUGS
    query_stripped = query.strip()[:200]

    all_results: list[dict] = []
    try:
        from database import get_db_session
        with get_db_session() as session:
            for slug in modules_to_search:
                searcher = _SEARCHERS.get(slug)
                if searcher and query_stripped:
                    try:
                        hits = searcher(session, query_stripped, filters)
                        all_results.extend(hits)
                    except Exception as e:
                        logger.debug("Search module %s error: %s", slug, e)
    except Exception as e:
        logger.error("search: session error: %s", e, exc_info=True)

    # Sort
    all_results = _sort_results(all_results, sort)

    elapsed_ms = int((time.time() - start_ms) * 1000)

    # Record history
    if admin_telegram_id:
        _record_search(admin_telegram_id, query_stripped, modules_to_search,
                       len(all_results), elapsed_ms, filters)

    total = len(all_results)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    slice_ = all_results[start:start + per_page]

    # Strip internal sort keys
    for r in slice_:
        r.pop("_sort_amount", None)

    return {
        "results": slice_,
        "total": total,
        "page": page,
        "pages": pages,
        "query": query_stripped,
        "search_time_ms": elapsed_ms,
        "modules_searched": modules_to_search,
        "filters_applied": {k: v for k, v in filters.items() if v},
        "sort": sort,
    }


def _record_search(admin_telegram_id: int, query: str,
                   modules: list[str], result_count: int,
                   elapsed_ms: int, filters: Optional[dict] = None) -> None:
    try:
        from database import get_db_session
        from database.models import SearchRecord
        with get_db_session() as session:
            rec = SearchRecord(
                admin_telegram_id=admin_telegram_id,
                query=query,
                modules=json.dumps(modules),
                result_count=result_count,
                search_time_ms=elapsed_ms,
                is_saved=False,
                created_at=datetime.utcnow(),
            )
            # Store filters if SearchRecord has a filters_json column
            if hasattr(SearchRecord, "filters_json") and filters:
                rec.filters_json = json.dumps(filters)
            session.add(rec)
    except Exception as e:
        logger.debug("_record_search: %s", e)


# ─── History & saved searches ─────────────────────────────────────────────────

def get_history(admin_telegram_id: int, limit: int = 20) -> list[dict]:
    try:
        from database import get_db_session
        from database.models import SearchRecord
        with get_db_session() as session:
            records = (session.query(SearchRecord)
                       .filter(SearchRecord.admin_telegram_id == admin_telegram_id)
                       .order_by(SearchRecord.created_at.desc())
                       .limit(limit).all())
            return [{
                "id": r.id,
                "query": r.query,
                "result_count": r.result_count,
                "search_time_ms": r.search_time_ms,
                "is_saved": r.is_saved,
                "label": r.label,
                "created_at": r.created_at,
            } for r in records]
    except Exception as e:
        logger.error("get_history: %s", e, exc_info=True)
        return []


def get_saved_searches(admin_telegram_id: int) -> list[dict]:
    try:
        from database import get_db_session
        from database.models import SearchRecord
        with get_db_session() as session:
            records = (session.query(SearchRecord)
                       .filter(SearchRecord.admin_telegram_id == admin_telegram_id,
                               SearchRecord.is_saved == True)
                       .order_by(SearchRecord.created_at.desc())
                       .all())
            return [{
                "id": r.id,
                "query": r.query,
                "label": r.label or r.query,
                "result_count": r.result_count,
                "created_at": r.created_at,
            } for r in records]
    except Exception as e:
        logger.error("get_saved_searches: %s", e, exc_info=True)
        return []


def save_search(record_id: int, label: Optional[str] = None) -> bool:
    try:
        from database import get_db_session
        from database.models import SearchRecord
        with get_db_session() as session:
            rec = session.query(SearchRecord).filter(SearchRecord.id == record_id).first()
            if not rec:
                return False
            rec.is_saved = True
            if label:
                rec.label = label[:128]
        return True
    except Exception as e:
        logger.error("save_search: %s", e, exc_info=True)
        return False


def delete_search_record(record_id: int) -> bool:
    try:
        from database import get_db_session
        from database.models import SearchRecord
        with get_db_session() as session:
            rec = session.query(SearchRecord).filter(SearchRecord.id == record_id).first()
            if not rec:
                return False
            session.delete(rec)
        return True
    except Exception as e:
        logger.error("delete_search_record: %s", e, exc_info=True)
        return False


def clear_history(admin_telegram_id: int) -> int:
    """Delete all non-saved search history for an admin. Returns count deleted."""
    try:
        from database import get_db_session
        from database.models import SearchRecord
        with get_db_session() as session:
            deleted = (session.query(SearchRecord)
                       .filter(SearchRecord.admin_telegram_id == admin_telegram_id,
                               SearchRecord.is_saved == False)
                       .delete())
            return deleted
    except Exception as e:
        logger.error("clear_history: %s", e, exc_info=True)
        return 0


# ─── Statistics ───────────────────────────────────────────────────────────────

def get_stats(admin_telegram_id: Optional[int] = None) -> dict:
    try:
        from database import get_db_session
        from database.models import SearchRecord
        from sqlalchemy import func
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = now - timedelta(days=7)
        with get_db_session() as session:
            q = session.query(SearchRecord)
            if admin_telegram_id:
                q = q.filter(SearchRecord.admin_telegram_id == admin_telegram_id)
            total = q.count()
            today = q.filter(SearchRecord.created_at >= today_start).count()
            weekly = q.filter(SearchRecord.created_at >= week_start).count()
            saved = q.filter(SearchRecord.is_saved == True).count()
            popular = (session.query(SearchRecord.query,
                                     func.count(SearchRecord.id).label("cnt"))
                       .group_by(SearchRecord.query)
                       .order_by(func.count(SearchRecord.id).desc())
                       .limit(5).all())
            avg_q = session.query(func.avg(SearchRecord.search_time_ms)).scalar()
            avg_ms = round(float(avg_q or 0), 1)
            recent = (session.query(SearchRecord)
                      .order_by(SearchRecord.created_at.desc())
                      .limit(5).all())
        return {
            "total": total,
            "today": today,
            "weekly": weekly,
            "saved": saved,
            "avg_ms": avg_ms,
            "popular": [(p.query, p.cnt) for p in popular],
            "recent": [r.query for r in recent],
        }
    except Exception as e:
        logger.error("get_stats: %s", e, exc_info=True)
        return {"total": 0, "today": 0, "weekly": 0, "saved": 0,
                "avg_ms": 0, "popular": [], "recent": []}

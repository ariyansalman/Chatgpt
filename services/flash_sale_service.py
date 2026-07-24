"""V38 — Flash Sale Manager Service.

Manages FlashSaleEvent records, applies/restores prices, dispatches
timed broadcasts, and provides stats helpers.

Public API
──────────
create(name, scope_type, product_ids, category_ids, discount_percent,
       fixed_sale_price, start_time, end_time, ...) → FlashSaleEvent | None
update(event_id, **fields) → bool
duplicate(event_id, created_by) → FlashSaleEvent | None
delete(event_id) → bool
pause(event_id) → bool
resume(event_id) → bool
end_now(event_id, bot=None) → bool

# Called by the scheduler every 60 seconds:
tick_flash_sales(bot) → None

# Display helpers:
get_active_sales_for_product(product_id) → list[dict]
get_homepage_banner() → dict | None
get_countdown_text(event_id) → str
get_stats() → dict
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from database import get_db_session
from database.models import (
    FlashSaleEvent, FlashSalePriceSnapshot, FlashSaleBroadcastLog,
    FlashSale, Product, Category,
)
from utils.bot_config import cfg

logger = logging.getLogger(__name__)

# ── Broadcast threshold definitions ──────────────────────────────────────────
# (broadcast_type, attribute_on_event, seconds_before_end)
_BROADCAST_THRESHOLDS = [
    ("24h",  "broadcast_24h",  86400),
    ("12h",  "broadcast_12h",  43200),
    ("6h",   "broadcast_6h",   21600),
    ("3h",   "broadcast_3h",   10800),
    ("1h",   "broadcast_1h",    3600),
    ("30m",  "broadcast_30m",   1800),
    ("10m",  "broadcast_10m",    600),
]

_SCOPE_SINGLE   = "single_product"
_SCOPE_MULTI    = "multi_product"
_SCOPE_CAT      = "category"
_SCOPE_MULTI_CAT = "multi_category"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    return cfg.get("flash_sale_manager_status", "enabled") == "enabled"


def _auto_price_update() -> bool:
    return cfg.get_bool("fsm_auto_price_update", True)


def _auto_broadcast() -> bool:
    return cfg.get_bool("fsm_auto_broadcast", True)


def _decode_ids(json_str: Optional[str]) -> List[int]:
    if not json_str:
        return []
    try:
        return [int(i) for i in json.loads(json_str)]
    except Exception:
        return []


def _encode_ids(ids: List[int]) -> str:
    return json.dumps([int(i) for i in ids])


def _compute_sale_price(product: Product, fse: FlashSaleEvent) -> float:
    base = float(product.price)
    if fse.discount_percent is not None:
        pct = max(0.0, min(100.0, float(fse.discount_percent)))
        return round(base * (1.0 - pct / 100.0), 4)
    if fse.fixed_sale_price is not None:
        return round(float(fse.fixed_sale_price), 4)
    return base


def _get_product_ids_for_event(session, fse: FlashSaleEvent) -> List[int]:
    """Resolve all product IDs covered by a FlashSaleEvent."""
    if fse.scope_type in (_SCOPE_SINGLE, _SCOPE_MULTI):
        return _decode_ids(fse.product_ids_json)
    # Category scope — fetch all active product IDs in the category/categories
    cat_ids = _decode_ids(fse.category_ids_json)
    if not cat_ids:
        return []
    rows = session.query(Product.id).filter(
        Product.category_id.in_(cat_ids),
        Product.is_active == True,  # noqa: E712
    ).all()
    return [r[0] for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Price application / restoration
# ─────────────────────────────────────────────────────────────────────────────

def _apply_prices(session, fse: FlashSaleEvent) -> int:
    """Set product.sale_price for all products in the event. Returns count."""
    pids = _get_product_ids_for_event(session, fse)
    applied = 0

    # Batch-fetch products and existing snapshots up front instead of
    # issuing two queries per product id (N+1) — matters at scale when a
    # flash sale spans hundreds/thousands of products.
    products_by_id = {
        p.id: p for p in session.query(Product).filter(Product.id.in_(pids)).all()
    } if pids else {}
    existing_snapshots = {
        snap.product_id
        for snap in session.query(FlashSalePriceSnapshot.product_id).filter_by(
            flash_sale_event_id=fse.id
        ).all()
    } if pids else set()

    for pid in pids:
        product = products_by_id.get(pid)
        if not product:
            continue
        sale_price = _compute_sale_price(product, fse)
        if sale_price >= float(product.price):
            continue  # skip if no actual discount

        # Save snapshot (INSERT OR IGNORE via unique constraint)
        existing = pid in existing_snapshots
        if not existing:
            snap = FlashSalePriceSnapshot(
                flash_sale_event_id=fse.id,
                product_id=pid,
                original_price=float(product.price),
                original_sale_price=product.sale_price,
                applied_sale_price=sale_price,
            )
            session.add(snap)

        product.sale_price = sale_price
        applied += 1

    # Also ensure a legacy FlashSale row exists for pricing.py compat
    _sync_legacy_flash_sale(session, fse, activate=True)
    return applied


def _restore_prices(session, fse: FlashSaleEvent) -> int:
    """Restore product.sale_price from snapshots. Returns count."""
    snaps = session.query(FlashSalePriceSnapshot).filter_by(
        flash_sale_event_id=fse.id
    ).all()
    restored = 0

    # Batch-fetch the products for all snapshots in one query instead of
    # one SELECT per snapshot (N+1).
    snap_pids = [snap.product_id for snap in snaps]
    products_by_id = {
        p.id: p for p in session.query(Product).filter(Product.id.in_(snap_pids)).all()
    } if snap_pids else {}

    for snap in snaps:
        product = products_by_id.get(snap.product_id)
        if not product:
            continue
        product.sale_price = snap.original_sale_price   # restore (may be None)
        session.delete(snap)
        restored += 1

    _sync_legacy_flash_sale(session, fse, activate=False)
    return restored


def _sync_legacy_flash_sale(session, fse: FlashSaleEvent, activate: bool) -> None:
    """Keep a legacy FlashSale row in sync so pricing.py works without changes."""
    try:
        # Use product/category ids for legacy compatibility
        product_ids = _decode_ids(fse.product_ids_json) if fse.scope_type in (
            _SCOPE_SINGLE, _SCOPE_MULTI) else []
        cat_ids = _decode_ids(fse.category_ids_json) if fse.scope_type in (
            _SCOPE_CAT, _SCOPE_MULTI_CAT) else []

        # Build a set of (product_id, category_id) tuples
        pairs: List[tuple] = []
        if product_ids:
            pairs = [(p, None) for p in product_ids]
        elif cat_ids:
            pairs = [(None, c) for c in cat_ids]

        for (prod_id, cat_id) in pairs:
            # Try to find existing legacy row
            q = session.query(FlashSale).filter(
                FlashSale.label == f"fsm_v38_{fse.id}",
            )
            if prod_id:
                q = q.filter(FlashSale.product_id == prod_id)
            if cat_id:
                q = q.filter(FlashSale.category_id == cat_id)
            legacy = q.first()

            if activate:
                pct = float(fse.discount_percent or 0)
                if legacy:
                    legacy.is_active = True
                    legacy.start_time = fse.start_time
                    legacy.end_time   = fse.end_time
                    legacy.discount_percent = pct
                else:
                    legacy = FlashSale(
                        product_id=prod_id,
                        category_id=cat_id,
                        discount_percent=pct,
                        start_time=fse.start_time,
                        end_time=fse.end_time,
                        is_active=True,
                        label=f"fsm_v38_{fse.id}",
                        created_by=fse.created_by,
                    )
                    session.add(legacy)
            else:
                if legacy:
                    legacy.is_active = False
    except Exception:
        logger.exception("_sync_legacy_flash_sale failed for fse_id=%s", fse.id)


# ─────────────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────────────

def create(
    name: str,
    scope_type: str,
    start_time: datetime,
    end_time: datetime,
    product_ids: Optional[List[int]] = None,
    category_ids: Optional[List[int]] = None,
    discount_percent: Optional[float] = None,
    fixed_sale_price: Optional[float] = None,
    description: Optional[str] = None,
    badge_text: Optional[str] = None,
    banner_file_id: Optional[str] = None,
    message_template: Optional[str] = None,
    timezone: str = "UTC",
    priority: int = 0,
    show_on_homepage: bool = True,
    broadcast_on_start: bool = True,
    broadcast_on_end: bool = False,
    broadcast_24h: bool = True,
    broadcast_12h: bool = False,
    broadcast_6h: bool = False,
    broadcast_3h: bool = False,
    broadcast_1h: bool = True,
    broadcast_30m: bool = False,
    broadcast_10m: bool = False,
    created_by: Optional[int] = None,
) -> Optional[FlashSaleEvent]:
    """Create a new FlashSaleEvent in DRAFT state."""
    try:
        # Validation
        if end_time <= start_time:
            raise ValueError("end_time must be after start_time")
        if discount_percent is None and fixed_sale_price is None:
            raise ValueError("Either discount_percent or fixed_sale_price must be set")
        if discount_percent is not None and not (0 < discount_percent < 100):
            raise ValueError("discount_percent must be between 0 and 100")
        if fixed_sale_price is not None and fixed_sale_price <= 0:
            raise ValueError("fixed_sale_price must be positive")

        template = message_template or cfg.get(
            "fsm_default_message_template",
            "⚡ <b>FLASH SALE</b>\n\n{product_name}\n\n"
            "${old_price} → <b>{sale_price}</b>\n"
            "🎁 Save {discount_percent}%\n\n⏰ Ends in: {countdown}"
        )

        with get_db_session() as s:
            fse = FlashSaleEvent(
                name=name[:255],
                description=description,
                banner_file_id=banner_file_id,
                badge_text=(badge_text or "⚡ FLASH SALE")[:64],
                scope_type=scope_type[:32],
                product_ids_json=_encode_ids(product_ids or []),
                category_ids_json=_encode_ids(category_ids or []),
                discount_percent=discount_percent,
                fixed_sale_price=fixed_sale_price,
                start_time=start_time,
                end_time=end_time,
                timezone=timezone[:64],
                priority=priority,
                status="draft",
                is_active=True,
                broadcast_on_start=broadcast_on_start,
                broadcast_on_end=broadcast_on_end,
                broadcast_24h=broadcast_24h,
                broadcast_12h=broadcast_12h,
                broadcast_6h=broadcast_6h,
                broadcast_3h=broadcast_3h,
                broadcast_1h=broadcast_1h,
                broadcast_30m=broadcast_30m,
                broadcast_10m=broadcast_10m,
                message_template=template,
                show_on_homepage=show_on_homepage,
                homepage_priority=0,
                created_by=created_by,
            )
            # Auto-schedule if start_time is in the future
            now = datetime.utcnow()
            if start_time <= now < end_time:
                fse.status = "active"
            elif start_time > now:
                fse.status = "scheduled"
            s.add(fse)
            s.commit()
            s.refresh(fse)
            return fse
    except Exception:
        logger.exception("flash_sale_service.create failed")
        return None


def update(event_id: int, **fields) -> bool:
    """Update mutable fields on a FlashSaleEvent. Returns True on success."""
    try:
        with get_db_session() as s:
            fse = s.query(FlashSaleEvent).filter_by(id=event_id).first()
            if not fse:
                return False
            ALLOWED = {
                "name", "description", "banner_file_id", "badge_text",
                "discount_percent", "fixed_sale_price", "start_time", "end_time",
                "timezone", "priority", "message_template", "show_on_homepage",
                "homepage_priority", "broadcast_on_start", "broadcast_on_end",
                "broadcast_24h", "broadcast_12h", "broadcast_6h",
                "broadcast_3h", "broadcast_1h", "broadcast_30m", "broadcast_10m",
            }
            for k, v in fields.items():
                if k in ALLOWED:
                    setattr(fse, k, v)
            fse.updated_at = datetime.utcnow()
            s.commit()
            return True
    except Exception:
        logger.exception("flash_sale_service.update failed for id=%s", event_id)
        return False


def duplicate(event_id: int, created_by: Optional[int] = None) -> Optional[FlashSaleEvent]:
    """Duplicate an existing FlashSaleEvent as DRAFT. Returns the new record."""
    try:
        with get_db_session() as s:
            src = s.query(FlashSaleEvent).filter_by(id=event_id).first()
            if not src:
                return None
            copy = FlashSaleEvent(
                name=f"Copy of {src.name}"[:255],
                description=src.description,
                banner_file_id=src.banner_file_id,
                badge_text=src.badge_text,
                scope_type=src.scope_type,
                product_ids_json=src.product_ids_json,
                category_ids_json=src.category_ids_json,
                discount_percent=src.discount_percent,
                fixed_sale_price=src.fixed_sale_price,
                start_time=src.start_time,
                end_time=src.end_time,
                timezone=src.timezone,
                priority=src.priority,
                status="draft",
                is_active=True,
                broadcast_on_start=src.broadcast_on_start,
                broadcast_on_end=src.broadcast_on_end,
                broadcast_24h=src.broadcast_24h,
                broadcast_12h=src.broadcast_12h,
                broadcast_6h=src.broadcast_6h,
                broadcast_3h=src.broadcast_3h,
                broadcast_1h=src.broadcast_1h,
                broadcast_30m=src.broadcast_30m,
                broadcast_10m=src.broadcast_10m,
                message_template=src.message_template,
                show_on_homepage=src.show_on_homepage,
                homepage_priority=src.homepage_priority,
                created_by=created_by or src.created_by,
            )
            s.add(copy)
            s.commit()
            s.refresh(copy)
            return copy
    except Exception:
        logger.exception("flash_sale_service.duplicate failed for id=%s", event_id)
        return None


def pause(event_id: int) -> bool:
    """Pause an active FlashSaleEvent (prices remain applied). Returns True on success."""
    try:
        with get_db_session() as s:
            fse = s.query(FlashSaleEvent).filter_by(id=event_id).first()
            if not fse or fse.status != "active":
                return False
            fse.status = "paused"
            fse.updated_at = datetime.utcnow()
            s.commit()
            return True
    except Exception:
        logger.exception("flash_sale_service.pause failed for id=%s", event_id)
        return False


def resume(event_id: int) -> bool:
    """Resume a paused FlashSaleEvent. Returns True on success."""
    try:
        with get_db_session() as s:
            fse = s.query(FlashSaleEvent).filter_by(id=event_id).first()
            if not fse or fse.status != "paused":
                return False
            now = datetime.utcnow()
            if now >= fse.end_time:
                fse.status = "ended"
            else:
                fse.status = "active"
            fse.updated_at = now
            s.commit()
            return True
    except Exception:
        logger.exception("flash_sale_service.resume failed for id=%s", event_id)
        return False


def end_now(event_id: int, bot=None) -> bool:
    """Immediately end a sale and restore prices. Returns True on success."""
    try:
        with get_db_session() as s:
            fse = s.query(FlashSaleEvent).filter_by(id=event_id).first()
            if not fse:
                return False
            if _auto_price_update():
                _restore_prices(s, fse)
            fse.status = "ended"
            fse.is_active = False
            fse.end_time = datetime.utcnow()
            fse.updated_at = datetime.utcnow()
            s.commit()
        # Send "ended" broadcast if configured
        if bot and _auto_broadcast():
            import asyncio
            try:
                asyncio.get_event_loop().create_task(
                    _send_broadcast(event_id, "end", bot)
                )
            except RuntimeError:
                pass
        return True
    except Exception:
        logger.exception("flash_sale_service.end_now failed for id=%s", event_id)
        return False


def delete_sale(event_id: int) -> bool:
    """Delete a FlashSaleEvent (restores prices first if active). Returns True on success."""
    try:
        with get_db_session() as s:
            fse = s.query(FlashSaleEvent).filter_by(id=event_id).first()
            if not fse:
                return False
            if fse.status in ("active", "paused") and _auto_price_update():
                _restore_prices(s, fse)
            s.delete(fse)
            s.commit()
            return True
    except Exception:
        logger.exception("flash_sale_service.delete failed for id=%s", event_id)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler tick — called every 60 seconds from bot.py
# ─────────────────────────────────────────────────────────────────────────────

async def tick_flash_sales(bot) -> None:
    """Main scheduler tick: activate pending sales, end expired ones, send broadcasts."""
    if not _is_enabled():
        return
    now = datetime.utcnow()

    try:
        with get_db_session() as s:
            # 1. Activate scheduled sales whose start_time has arrived
            to_activate = s.query(FlashSaleEvent).filter(
                FlashSaleEvent.status == "scheduled",
                FlashSaleEvent.is_active == True,  # noqa: E712
                FlashSaleEvent.start_time <= now,
                FlashSaleEvent.end_time > now,
            ).all()
            for fse in to_activate:
                try:
                    if _auto_price_update():
                        _apply_prices(s, fse)
                    fse.status = "active"
                    fse.updated_at = now
                    logger.info("Flash sale started: id=%s name=%s", fse.id, fse.name)
                except Exception:
                    logger.exception("Flash sale start failed: id=%s", fse.id)
            s.commit()

            # Send start broadcasts (outside the session lock)
            for fse in to_activate:
                if _auto_broadcast() and fse.broadcast_on_start:
                    await _send_broadcast(fse.id, "start", bot)

            # 2. End active sales whose end_time has passed
            to_end = s.query(FlashSaleEvent).filter(
                FlashSaleEvent.status.in_(["active", "paused"]),
                FlashSaleEvent.is_active == True,  # noqa: E712
                FlashSaleEvent.end_time <= now,
            ).all()
            for fse in to_end:
                try:
                    if _auto_price_update():
                        _restore_prices(s, fse)
                    fse.status = "ended"
                    fse.is_active = False
                    fse.updated_at = now
                    logger.info("Flash sale ended: id=%s name=%s", fse.id, fse.name)
                except Exception:
                    logger.exception("Flash sale end failed: id=%s", fse.id)
            s.commit()

            for fse in to_end:
                if _auto_broadcast() and fse.broadcast_on_end:
                    await _send_broadcast(fse.id, "end", bot)

            # 3. Timed countdown broadcasts for active/scheduled sales
            if _auto_broadcast():
                active_sales = s.query(FlashSaleEvent).filter(
                    FlashSaleEvent.status.in_(["active", "scheduled"]),
                    FlashSaleEvent.is_active == True,  # noqa: E712
                    FlashSaleEvent.end_time > now,
                ).all()
                for fse in active_sales:
                    seconds_left = (fse.end_time - now).total_seconds()
                    for bc_type, bc_attr, threshold in _BROADCAST_THRESHOLDS:
                        if not getattr(fse, bc_attr, False):
                            continue
                        # Fire within a 90-second window to avoid missing the tick
                        if threshold - 90 <= seconds_left <= threshold + 90:
                            # Check not already sent
                            already = s.query(FlashSaleBroadcastLog).filter_by(
                                flash_sale_event_id=fse.id,
                                broadcast_type=bc_type,
                            ).first()
                            if not already:
                                await _send_broadcast(fse.id, bc_type, bot)

    except Exception:
        logger.exception("flash_sale_service.tick_flash_sales failed")


async def _send_broadcast(event_id: int, bc_type: str, bot) -> None:
    """Send a flash sale broadcast and record it in FlashSaleBroadcastLog."""
    try:
        from database import get_db_session as _gs
        from database.models import User as _User, BotConfig as _BotConfig
        from utils.bot_config import cfg as _cfg

        with _gs() as s:
            fse = s.query(FlashSaleEvent).filter_by(id=event_id).first()
            if not fse:
                return

            # Build message using the template
            template = fse.message_template or _cfg.get(
                "fsm_default_message_template", "⚡ <b>FLASH SALE</b>\n\n{product_name}"
            )

            # Resolve representative product name and pricing for the message
            pids = _get_product_ids_for_event(s, fse)
            prod_name = fse.name
            old_price = ""
            sale_price_str = ""
            discount_str = ""
            if pids:
                prod = s.query(Product).filter_by(id=pids[0]).first()
                if prod:
                    prod_name = prod.name
                    old_price_val = float(prod.price)
                    applied = fse.discount_percent
                    if applied:
                        sp = round(old_price_val * (1.0 - applied / 100.0), 2)
                        discount_str = f"{applied:.0f}"
                        old_price = f"{old_price_val:.2f}"
                        sale_price_str = f"{sp:.2f}"
                    elif fse.fixed_sale_price:
                        sp = fse.fixed_sale_price
                        sale_price_str = f"{sp:.2f}"
                        old_price = f"{old_price_val:.2f}"
                        if old_price_val > 0:
                            discount_str = f"{(1 - sp/old_price_val)*100:.0f}"

            countdown = fse.countdown()

            text = template.format(
                product_name=prod_name,
                old_price=old_price,
                sale_price=sale_price_str,
                discount_percent=discount_str,
                countdown=countdown,
                badge=fse.badge_text or "⚡ FLASH SALE",
                sale_name=fse.name,
            )

            # Fetch all users that should receive the broadcast
            users = s.query(_User).filter(
                _User.is_active == True,  # noqa: E712
                _User.telegram_id.isnot(None),
            ).all()

            sent = 0
            for user in users:
                try:
                    if not user.telegram_id:
                        continue
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    sent += 1
                except Exception:
                    pass

            # Record broadcast sent
            existing = s.query(FlashSaleBroadcastLog).filter_by(
                flash_sale_event_id=event_id,
                broadcast_type=bc_type,
            ).first()
            if not existing:
                log = FlashSaleBroadcastLog(
                    flash_sale_event_id=event_id,
                    broadcast_type=bc_type,
                    recipients=sent,
                )
                s.add(log)
                s.commit()
            logger.info(
                "Flash sale broadcast sent: fse_id=%s type=%s recipients=%s",
                event_id, bc_type, sent
            )
    except Exception:
        logger.exception("_send_broadcast failed: fse_id=%s type=%s", event_id, bc_type)


# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_active_sales_for_product(product_id: int) -> List[Dict]:
    """Return list of currently-live flash sale details for a product (for badges/display)."""
    try:
        now = datetime.utcnow()
        with get_db_session() as s:
            product = s.query(Product).filter_by(id=product_id).first()
            if not product:
                return []

            # Check product-scope sales
            results: List[FlashSaleEvent] = []
            prod_sales = s.query(FlashSaleEvent).filter(
                FlashSaleEvent.status == "active",
                FlashSaleEvent.is_active == True,  # noqa: E712
                FlashSaleEvent.start_time <= now,
                FlashSaleEvent.end_time > now,
            ).all()

            for fse in prod_sales:
                pids = _get_product_ids_for_event(s, fse)
                if product_id in pids:
                    results.append(fse)

            out = []
            for fse in results:
                base = float(product.price)
                computed_sp = _compute_sale_price(product, fse)
                discount_pct = round((1 - computed_sp / base) * 100, 1) if base > 0 else 0
                out.append({
                    "id": fse.id,
                    "name": fse.name,
                    "badge_text": fse.badge_text or "⚡ FLASH SALE",
                    "old_price": base,
                    "sale_price": computed_sp,
                    "discount_percent": discount_pct,
                    "countdown": fse.countdown(now),
                    "end_time": fse.end_time.isoformat(),
                    "description": fse.description,
                })
            return out
    except Exception:
        logger.exception("get_active_sales_for_product failed for id=%s", product_id)
        return []


def get_homepage_banner() -> Optional[Dict]:
    """Return the highest-priority active flash sale for the homepage banner."""
    if not cfg.get_bool("fsm_homepage_banner", True):
        return None
    try:
        now = datetime.utcnow()
        with get_db_session() as s:
            fse = s.query(FlashSaleEvent).filter(
                FlashSaleEvent.status == "active",
                FlashSaleEvent.is_active == True,  # noqa: E712
                FlashSaleEvent.show_on_homepage == True,  # noqa: E712
                FlashSaleEvent.start_time <= now,
                FlashSaleEvent.end_time > now,
            ).order_by(
                FlashSaleEvent.homepage_priority.desc(),
                FlashSaleEvent.priority.desc(),
            ).first()
            if not fse:
                return None
            return {
                "id": fse.id,
                "name": fse.name,
                "badge_text": fse.badge_text or "⚡ FLASH SALE",
                "countdown": fse.countdown(now),
                "description": fse.description,
                "banner_file_id": fse.banner_file_id,
                "discount_percent": fse.discount_percent,
            }
    except Exception:
        logger.exception("get_homepage_banner failed")
        return None


def get_countdown_text(event_id: int) -> str:
    """Return countdown string for a specific event."""
    try:
        with get_db_session() as s:
            fse = s.query(FlashSaleEvent).filter_by(id=event_id).first()
            if not fse:
                return "N/A"
            return fse.countdown()
    except Exception:
        return "N/A"


def get_product_flash_badge(product_id: int) -> str:
    """Return a formatted flash sale badge for a product page (empty string if none)."""
    if not cfg.get_bool("fsm_product_badge", True):
        return ""
    sales = get_active_sales_for_product(product_id)
    if not sales:
        return ""
    s = sales[0]  # highest priority
    lines = [
        f"\n{s['badge_text']}  🔥 <b>Limited Time Offer!</b>",
        f"$<s>{s['old_price']:.2f}</s> → <b>{s['sale_price']:.2f}</b>",
        f"🎁 Save <b>{s['discount_percent']:.0f}%</b>",
        f"⏰ Ends in: <b>{s['countdown']}</b>",
    ]
    return "\n".join(lines)


def get_stats() -> Dict:
    """Return aggregate Flash Sale Manager statistics."""
    try:
        with get_db_session() as s:
            now = datetime.utcnow()
            total     = s.query(FlashSaleEvent).count()
            active    = s.query(FlashSaleEvent).filter_by(status="active").count()
            scheduled = s.query(FlashSaleEvent).filter_by(status="scheduled").count()
            paused    = s.query(FlashSaleEvent).filter_by(status="paused").count()
            ended     = s.query(FlashSaleEvent).filter_by(status="ended").count()
            draft     = s.query(FlashSaleEvent).filter_by(status="draft").count()
            cancelled = s.query(FlashSaleEvent).filter_by(status="cancelled").count()
            revenue   = s.query(FlashSaleEvent).all()
            total_rev = sum(float(r.revenue or 0) for r in revenue)
            total_ord = sum(int(r.order_count or 0) for r in revenue)
            total_views = sum(int(r.view_count or 0) for r in revenue)
            total_clicks = sum(int(r.click_count or 0) for r in revenue)
            return {
                "total": total, "active": active, "scheduled": scheduled,
                "paused": paused, "ended": ended, "draft": draft,
                "cancelled": cancelled, "revenue": total_rev,
                "total_orders": total_ord, "total_views": total_views,
                "total_clicks": total_clicks,
            }
    except Exception:
        return {
            "total": 0, "active": 0, "scheduled": 0, "paused": 0,
            "ended": 0, "draft": 0, "cancelled": 0, "revenue": 0.0,
            "total_orders": 0, "total_views": 0, "total_clicks": 0,
        }


def get_best_selling_sale() -> Optional[Dict]:
    """Return the flash sale with the highest order_count."""
    try:
        with get_db_session() as s:
            fse = s.query(FlashSaleEvent).order_by(
                FlashSaleEvent.order_count.desc()
            ).first()
            if not fse:
                return None
            return {
                "id": fse.id, "name": fse.name,
                "order_count": fse.order_count, "revenue": fse.revenue,
            }
    except Exception:
        return None


async def flash_sale_scheduler_job(context) -> None:
    """APScheduler-compatible wrapper for tick_flash_sales."""
    bot = context.bot
    await tick_flash_sales(bot)

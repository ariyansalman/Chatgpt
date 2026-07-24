"""VIP Tier service — auto-upgrade / downgrade / cashback logic — V41."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy import func as sqlfunc
from database.models import (
    VipTier, UserVipTier, VipTierHistory, User,
    LoyaltyLedger, LoyaltyReward, LoyaltyRewardClaim,
)
from utils.bot_config import cfg

# Lazy import for Order to avoid circular dependency at module load time
def _get_order_model():
    from database.models import Order  # noqa: PLC0415
    return Order

logger = logging.getLogger(__name__)


# ─── status helpers ──────────────────────────────────────────────────────────

def vip_enabled() -> bool:
    return cfg.get_str("vip_status", "enabled").lower() == "enabled"


def vip_maintenance() -> bool:
    return cfg.get_str("vip_status", "enabled").lower() == "maintenance"


# ─── tier resolution ─────────────────────────────────────────────────────────

def get_user_tier(session, user_id: int) -> Optional[VipTier]:
    """Return the current VipTier for a user, or the default tier."""
    assignment = (
        session.query(UserVipTier)
        .filter_by(user_id=user_id)
        .first()
    )
    if assignment:
        return session.query(VipTier).filter_by(id=assignment.tier_id).first()
    return _get_default_tier(session)


def _get_default_tier(session) -> Optional[VipTier]:
    t = session.query(VipTier).filter_by(is_default=True, is_active=True).first()
    if not t:
        t = session.query(VipTier).filter_by(is_active=True).order_by(VipTier.level).first()
    return t


def _compute_eligible_tier(session, user: User) -> Optional[VipTier]:
    """Return the highest tier the user qualifies for based on stats."""
    tiers = (
        session.query(VipTier)
        .filter_by(is_active=True)
        .order_by(VipTier.level.desc())
        .all()
    )
    now = datetime.utcnow()
    account_age_days = (now - user.created_at).days if user.created_at else 0
    Order = _get_order_model()
    total_orders = session.query(sqlfunc.count()).select_from(Order).filter_by(
        user_id=user.id
    ).scalar() or 0
    total_spending = float(user.total_spent or 0)
    referral_earnings = float(user.referral_balance or 0)

    for tier in tiers:
        if tier.level == 0:
            return tier  # default — always qualifies
        meets = (
            (tier.min_orders == 0 or total_orders >= tier.min_orders) and
            (tier.min_spending == 0 or total_spending >= tier.min_spending) and
            (tier.min_referral_earnings == 0 or referral_earnings >= tier.min_referral_earnings) and
            (tier.min_account_age_days == 0 or account_age_days >= tier.min_account_age_days)
        )
        if meets:
            return tier
    return _get_default_tier(session)


def _set_tier(session, user: User, new_tier: VipTier,
              changed_by: Optional[int] = None, reason: str = "auto") -> None:
    """Assign (or update) a user's VIP tier and write history."""
    assignment = session.query(UserVipTier).filter_by(user_id=user.id).first()
    old_tier_id = assignment.tier_id if assignment else None

    if assignment:
        if assignment.tier_id == new_tier.id:
            return  # no change
        assignment.tier_id = new_tier.id
        assignment.assigned_at = datetime.utcnow()
        assignment.assigned_by = changed_by
        assignment.reason = reason
    else:
        assignment = UserVipTier(
            user_id=user.id,
            tier_id=new_tier.id,
            assigned_by=changed_by,
            reason=reason,
        )
        session.add(assignment)

    history = VipTierHistory(
        user_id=user.id,
        old_tier_id=old_tier_id,
        new_tier_id=new_tier.id,
        reason=reason,
        changed_by=changed_by,
    )
    session.add(history)


# ─── auto-upgrade / downgrade ─────────────────────────────────────────────────

def check_and_upgrade(session, user: User, changed_by: Optional[int] = None) -> Optional[VipTier]:
    """Check if a user qualifies for a higher tier and upgrade if so.

    Returns the new tier if an upgrade happened, else None.
    Called from loyalty_handlers.award_loyalty_points (non-breaking addition).
    """
    if not cfg.get_bool("vip_auto_upgrade", True):
        return None
    if not vip_enabled():
        return None
    try:
        eligible = _compute_eligible_tier(session, user)
        if eligible is None:
            return None
        current = get_user_tier(session, user.id)
        if current is None or eligible.level > current.level:
            _set_tier(session, user, eligible, changed_by=changed_by, reason="auto_upgrade")
            return eligible
    except Exception:
        logger.exception("check_and_upgrade failed for user %s", user.id)
    return None


def admin_set_tier(session, user: User, tier: VipTier, admin_tg_id: int,
                   reason: str = "manual") -> None:
    """Admin manually promotes or demotes a user."""
    _set_tier(session, user, tier, changed_by=admin_tg_id, reason=reason)


# ─── loyalty points ──────────────────────────────────────────────────────────

def award_points(session, user: User, pts: int, reason: str,
                 order_id: Optional[int] = None) -> None:
    """Award loyalty points from any source (orders, deposits, referrals, bonuses)."""
    if pts <= 0:
        return
    user.loyalty_points = (user.loyalty_points or 0) + pts
    session.add(LoyaltyLedger(
        user_id=user.id, change=pts,
        balance_after=user.loyalty_points,
        reason=reason, order_id=order_id,
    ))


def deduct_points(session, user: User, pts: int, reason: str) -> bool:
    """Deduct points from a user. Returns False if insufficient balance."""
    if (user.loyalty_points or 0) < pts:
        return False
    user.loyalty_points = user.loyalty_points - pts
    session.add(LoyaltyLedger(
        user_id=user.id, change=-pts,
        balance_after=user.loyalty_points,
        reason=reason,
    ))
    return True


def reset_points(session, user: User, admin_tg_id: int) -> None:
    """Reset a user's points to zero."""
    pts = user.loyalty_points or 0
    if pts > 0:
        session.add(LoyaltyLedger(
            user_id=user.id, change=-pts,
            balance_after=0,
            reason=f"admin_reset by {admin_tg_id}",
        ))
    user.loyalty_points = 0


# ─── reward redemption ───────────────────────────────────────────────────────

def claim_reward(session, user: User, reward: LoyaltyReward) -> Tuple[bool, str]:
    """Attempt to claim a reward. Returns (success, message)."""
    now = datetime.utcnow()

    if not reward.is_active:
        return False, "❌ This reward is no longer available."
    if reward.expires_at and reward.expires_at < now:
        return False, "❌ This reward has expired."
    if reward.max_total_claims > 0 and reward.total_claims >= reward.max_total_claims:
        return False, "❌ This reward has reached its maximum total claims."

    # tier check
    current_tier = get_user_tier(session, user.id)
    current_level = current_tier.level if current_tier else 0
    if current_level < reward.min_tier_level:
        required_tier = (
            session.query(VipTier)
            .filter(VipTier.level >= reward.min_tier_level, VipTier.is_active == True)  # noqa: E712
            .order_by(VipTier.level)
            .first()
        )
        tier_name = f"{required_tier.emoji} {required_tier.name}" if required_tier else f"Level {reward.min_tier_level}"
        return False, f"❌ This reward requires {tier_name} tier or higher."

    # per-user claim limit
    if reward.max_claims_per_user > 0:
        user_claims = (
            session.query(sqlfunc.count())
            .select_from(LoyaltyRewardClaim)
            .filter_by(user_id=user.id, reward_id=reward.id)
            .scalar() or 0
        )
        if user_claims >= reward.max_claims_per_user:
            return False, f"❌ You have already claimed this reward {reward.max_claims_per_user}× (limit reached)."

    # daily limit
    daily_limit = cfg.get_int("vip_reward_limit_per_day", 0)
    if daily_limit > 0:
        day_start = datetime(now.year, now.month, now.day)
        day_claims = (
            session.query(sqlfunc.count())
            .select_from(LoyaltyRewardClaim)
            .filter(
                LoyaltyRewardClaim.user_id == user.id,
                LoyaltyRewardClaim.created_at >= day_start,
            )
            .scalar() or 0
        )
        if day_claims >= daily_limit:
            return False, f"❌ Daily reward claim limit ({daily_limit}) reached. Try again tomorrow."

    # points check
    if not deduct_points(session, user, reward.points_cost, f"reward_claim:{reward.id}"):
        return False, f"❌ Insufficient points. You need {reward.points_cost} pts (you have {user.loyalty_points or 0})."

    # apply reward
    value_received = reward.value
    if reward.reward_type == "wallet":
        user.wallet_balance = (user.wallet_balance or 0) + value_received
        session.add(LoyaltyLedger(
            user_id=user.id, change=0,
            balance_after=user.loyalty_points or 0,
            reason=f"reward_wallet:{reward.id}",
        ))
    # coupon / discount / product types are logged here; coupon issuance is
    # handled by the caller after this function returns.

    claim = LoyaltyRewardClaim(
        user_id=user.id,
        reward_id=reward.id,
        points_spent=reward.points_cost,
        value_received=value_received,
    )
    session.add(claim)
    reward.total_claims += 1
    return True, f"✅ Reward claimed! You received {_format_reward_value(reward)}."


def _format_reward_value(reward: LoyaltyReward) -> str:
    if reward.reward_type == "wallet":
        return f"${reward.value:.2f} wallet credit"
    if reward.reward_type == "discount":
        return f"{reward.value:.0f}% discount"
    if reward.reward_type == "coupon":
        return f"${reward.value:.2f} coupon"
    if reward.reward_type == "product":
        return "a free product"
    return f"{reward.value}"


# ─── statistics ───────────────────────────────────────────────────────────────

def get_vip_stats(session) -> dict:
    """Aggregate VIP statistics for the admin dashboard."""
    total_vip = session.query(sqlfunc.count(UserVipTier.id)).scalar() or 0
    total_points = session.query(sqlfunc.sum(User.loyalty_points)).scalar() or 0
    redeemed_pts = session.query(
        sqlfunc.sum(sqlfunc.abs(LoyaltyLedger.change))
    ).filter(LoyaltyLedger.change < 0).scalar() or 0
    total_claims = session.query(sqlfunc.count(LoyaltyRewardClaim.id)).scalar() or 0

    # tier distribution
    tier_dist_rows = (
        session.query(VipTier.name, VipTier.emoji, sqlfunc.count(UserVipTier.id))
        .outerjoin(UserVipTier, UserVipTier.tier_id == VipTier.id)
        .group_by(VipTier.id, VipTier.name, VipTier.emoji, VipTier.level)
        .order_by(VipTier.level)
        .all()
    )
    tier_dist = [(name, emoji, count) for name, emoji, count in tier_dist_rows]

    # top spenders
    top_spenders = (
        session.query(User)
        .filter(User.total_spent > 0)
        .order_by(User.total_spent.desc())
        .limit(5)
        .all()
    )

    return {
        "total_vip": total_vip,
        "total_points": int(total_points),
        "redeemed_pts": int(redeemed_pts),
        "total_claims": total_claims,
        "tier_dist": tier_dist,
        "top_spenders": top_spenders,
    }

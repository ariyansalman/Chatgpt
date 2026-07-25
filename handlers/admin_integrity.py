"""Integrity Center admin panel — trigger scans, view results."""
from __future__ import annotations

import json
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_db_session
from database.models import IntegrityScan
from services import integrity
from utils.audit import log_admin_action
from ._acc_helpers import require_admin, back_root, send


@require_admin
async def integrity_menu(update, context):
    with get_db_session() as s:
        latest = (s.query(IntegrityScan)
                   .order_by(IntegrityScan.started_at.desc()).first())
    lines = ["🩺 <b>INTEGRITY CENTER</b>",
             "Read-only scans. Repairs are separate, admin-confirmed actions.", ""]
    if latest:
        lines += [
            f"<b>Latest scan #{latest.id}</b>",
            f"When: {latest.completed_at or latest.started_at}",
            f"Checks: {latest.total_checks}  ·  Issues: {latest.total_issues}",
            f"CRITICAL: {latest.critical_count}  ·  WARNING: {latest.warning_count}  ·  INFO: {latest.info_count}",
        ]
    else:
        lines.append("No scans yet.")

    kb = [
        [InlineKeyboardButton("▶️ Run scan now", callback_data="acc:intg:run")],
    ]
    if latest:
        kb.append([InlineKeyboardButton("📋 View latest results",
                    callback_data=f"acc:intg:view:{latest.id}")])
    kb.append([back_root()])
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


async def _run(update):
    scan = integrity.run_scan(triggered_by="manual",
                              admin_id=update.effective_user.id)
    try:
        log_admin_action(update.effective_user.id, "integrity_scan",
                         f"scan_id={scan.id} issues={scan.total_issues}")
    except Exception:
        pass
    await _view(update, scan.id)


async def _view(update, scan_id: int):
    with get_db_session() as s:
        scan = s.get(IntegrityScan, scan_id)
        if not scan:
            await send(update, "Not found.", InlineKeyboardMarkup([[back_root()]]))
            return
        results = list(scan.results)

    lines = [f"🩺 <b>Scan #{scan.id}</b>",
             f"When: {scan.completed_at or scan.started_at}",
             f"Total issues: {scan.total_issues}", ""]
    for r in sorted(results, key=lambda x: {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
                    .get(x.severity, 3)):
        badge = {"CRITICAL": "🔴", "WARNING": "🟠", "INFO": "🔵"}.get(r.severity, "•")
        lines.append(f"{badge} <b>{r.check_name}</b> — count {r.count}")
        lines.append(f"    {r.explanation or ''}")
        try:
            ids = json.loads(r.sample_ids or "[]")
        except Exception:
            ids = []
        if ids:
            lines.append(f"    sample ids: {', '.join(str(x) for x in ids[:10])}")
    kb = [
        [InlineKeyboardButton("🔄 Run again", callback_data="acc:intg:run"),
         back_root()],
    ]
    await send(update, "\n".join(lines), InlineKeyboardMarkup(kb))


async def route(action, rest, update, context):
    if action == "run":
        await _run(update)
    elif action == "view" and rest:
        await _view(update, int(rest[0]))

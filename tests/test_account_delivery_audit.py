"""Audit tests for the Account / Login delivery system.

Covers all issues identified in the 2026-07-27 audit:
  1. format_account_delivery handles JSON-format stock (pipe was the only prior format).
  2. parse_delivery_asset correctly maps 4-field pipe format (email|pw|recovery|twofa)
     — previously fields 3 & 4 were swapped vs inventory_import.format_account_delivery.
  3. parse_key_value normalises "2fa" → also usable as "twofa" in templates.
  4. format_multi_account_delivery never exposes raw "|" separators.
  5. build_account_delivery_file handles JSON-format account values.
  6. render_template drops lines with all-blank placeholders (missing field hidden).
  7. accdel_show_* settings are applied in build_delivery_message.
  8. Backward compatibility: all pre-existing pipe-format tests still pass.
"""
from __future__ import annotations

import json
import os
import unittest

# ── ensure DATABASE_URL is set before any imports that trigger DB init ───────
os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# ─────────────────────────────────────────────────────────────────────────────
# 1. format_account_delivery — JSON format support
# ─────────────────────────────────────────────────────────────────────────────

class FormatAccountDeliveryJsonTest(unittest.TestCase):
    """format_account_delivery must handle JSON-format stock without exposing
    raw JSON or pipe separators to the buyer."""

    def _fad(self, value, **kw):
        from services.inventory_import import format_account_delivery
        return format_account_delivery(value, **kw)

    def test_json_two_fields(self):
        raw = json.dumps({"email": "a@b.com", "password": "pw123"})
        out = self._fad(raw)
        self.assertIn("📧 Email: a@b.com", out)
        self.assertIn("🔑 Password: pw123", out)
        self.assertNotIn("{", out)
        self.assertNotIn("|", out)

    def test_json_three_fields_with_twofa_key(self):
        raw = json.dumps({"email": "a@b.com", "password": "pw123", "twofa": "123456"})
        out = self._fad(raw)
        self.assertIn("🔐 2FA: 123456", out)
        self.assertNotIn("{", out)

    def test_json_legacy_2fa_key_is_accepted(self):
        """Stored with "2fa" key (old spelling) must still show 2FA line."""
        raw = json.dumps({"email": "a@b.com", "password": "pw", "2fa": "654321"})
        out = self._fad(raw)
        self.assertIn("654321", out)
        self.assertNotIn("{", out)

    def test_json_with_recovery_email(self):
        raw = json.dumps({"email": "a@b.com", "password": "pw", "recovery": "rec@b.com"})
        out = self._fad(raw)
        self.assertIn("📨 Recovery Email: rec@b.com", out)

    def test_json_show_2fa_false_hides_twofa(self):
        raw = json.dumps({"email": "a@b.com", "password": "pw", "twofa": "999"})
        out = self._fad(raw, show_2fa=False)
        self.assertNotIn("999", out)
        self.assertNotIn("🔐", out)

    def test_json_compact_layout(self):
        raw = json.dumps({"email": "a@b.com", "password": "pw", "twofa": "111"})
        out = self._fad(raw, compact=True)
        self.assertNotIn("\n", out)  # compact = single line
        self.assertNotIn("|", out)

    def test_json_missing_password_renders_gracefully(self):
        """Values with no email/password should not blow up."""
        raw = json.dumps({"api_key": "sk_test_12345", "region": "EU"})
        out = self._fad(raw)
        # Should still produce some output without crashing
        self.assertIsInstance(out, str)
        self.assertNotIn("{", out)

    def test_pipe_format_unchanged(self):
        """Existing pipe-delimited stock must still format exactly as before."""
        from services.inventory_import import format_account_delivery
        out = format_account_delivery("user@example.com|Pass123@|abcd efgh")
        self.assertEqual(
            out,
            "📧 Email: user@example.com\n🔑 Password: Pass123@\n🔐 2FA: abcd efgh",
        )

    def test_four_field_pipe_format(self):
        """4-field format: email|password|recovery|twofa — no raw separators shown."""
        out = self._fad("a@b.com|pw|rec@b.com|9876")
        self.assertIn("📧 Email: a@b.com", out)
        self.assertIn("🔑 Password: pw", out)
        self.assertIn("📨 Recovery Email: rec@b.com", out)
        self.assertIn("🔐 Recovery Code: 9876", out)
        self.assertNotIn("|", out)

    def test_single_value_no_pipe_returned_as_is(self):
        out = self._fad("SIMPLE-LICENSE-KEY-1234")
        self.assertEqual(out, "SIMPLE-LICENSE-KEY-1234")


# ─────────────────────────────────────────────────────────────────────────────
# 2. parse_delivery_asset — 4-field pipe order consistency
# ─────────────────────────────────────────────────────────────────────────────

class ParseDeliveryAssetPipeOrderTest(unittest.TestCase):
    """parse_delivery_asset must map the 4-field pipe format as
    email|password|RECOVERY|TWOFA — consistent with format_account_delivery."""

    def _parse(self, raw):
        from services.delivery_message_renderer import parse_delivery_asset
        return parse_delivery_asset(raw)

    def test_three_field_pipe_twofa_in_field3(self):
        fields = self._parse("a@b.com|pw|123456")
        self.assertEqual(fields.get("email"), "a@b.com")
        self.assertEqual(fields.get("password"), "pw")
        self.assertEqual(fields.get("twofa"), "123456")
        self.assertNotIn("recovery", fields)

    def test_four_field_pipe_recovery_field3_twofa_field4(self):
        """Fields 3 & 4 must be RECOVERY then TWOFA (not the other way around)."""
        fields = self._parse("a@b.com|pw|rec@b.com|654321")
        self.assertEqual(fields.get("email"), "a@b.com")
        self.assertEqual(fields.get("password"), "pw")
        self.assertEqual(fields.get("recovery"), "rec@b.com")
        self.assertEqual(fields.get("twofa"), "654321")

    def test_json_format_normalises_2fa_key(self):
        raw = json.dumps({"email": "x@y.com", "password": "pw", "2fa": "111222"})
        fields = self._parse(raw)
        self.assertEqual(fields.get("twofa"), "111222")

    def test_empty_returns_empty_dict(self):
        self.assertEqual(self._parse(""), {})
        self.assertEqual(self._parse(None), {})

    def test_raw_key_returns_empty_dict(self):
        """A plain single value with no | or JSON returns no named fields."""
        fields = self._parse("PLAIN-KEY-XYZ")
        self.assertEqual(fields, {})


# ─────────────────────────────────────────────────────────────────────────────
# 3. structured_delivery.parse_key_value — "2fa" / "twofa" aliasing
# ─────────────────────────────────────────────────────────────────────────────

class ParseKeyValueJsonNormalisationTest(unittest.TestCase):
    def _pkv(self, raw, placeholders=None):
        from services.structured_delivery import parse_key_value
        return parse_key_value(raw, placeholders)

    def test_json_2fa_key_creates_twofa_alias(self):
        raw = json.dumps({"email": "a@b.com", "password": "pw", "2fa": "999888"})
        fields = self._pkv(raw)
        # Both spellings must resolve
        self.assertEqual(fields.get("2fa"), "999888")
        self.assertEqual(fields.get("twofa"), "999888")

    def test_json_twofa_key_creates_2fa_alias(self):
        raw = json.dumps({"email": "a@b.com", "password": "pw", "twofa": "777666"})
        fields = self._pkv(raw)
        self.assertEqual(fields.get("twofa"), "777666")
        self.assertEqual(fields.get("2fa"), "777666")

    def test_template_with_twofa_placeholder_works_with_2fa_json_key(self):
        from services.structured_delivery import render_delivery_message
        template = "Email: {email}\n2FA: {twofa}"
        raw = json.dumps({"email": "x@y.com", "password": "pw", "2fa": "123456"})
        rendered = render_delivery_message(template, raw)
        self.assertIn("2FA: 123456", rendered)

    def test_template_blank_line_dropped_when_twofa_missing(self):
        from services.structured_delivery import render_delivery_message
        template = "Email: {email}\n2FA: {twofa}"
        raw = json.dumps({"email": "x@y.com", "password": "pw"})
        rendered = render_delivery_message(template, raw)
        self.assertNotIn("{twofa}", rendered)
        self.assertNotIn("2FA:", rendered)  # line should be dropped


# ─────────────────────────────────────────────────────────────────────────────
# 4. format_multi_account_delivery — never expose raw "|"
# ─────────────────────────────────────────────────────────────────────────────

class FormatMultiAccountDeliveryTest(unittest.TestCase):
    def _fmt(self, values):
        from services.inventory_import import format_multi_account_delivery
        return format_multi_account_delivery(values)

    def test_single_account_no_separator(self):
        out = self._fmt(["a@b.com|pw|totp"])
        self.assertNotIn("|", out)
        self.assertIn("📧 Email: a@b.com", out)

    def test_single_json_account_no_raw_json(self):
        raw = json.dumps({"email": "a@b.com", "password": "pw"})
        out = self._fmt([raw])
        self.assertNotIn("{", out)
        self.assertNotIn("|", out)
        self.assertIn("a@b.com", out)

    def test_multi_account_no_raw_pipe(self):
        values = ["a@b.com|pw1|2fa1", "b@b.com|pw2|2fa2"]
        out = self._fmt(values)
        self.assertNotIn("|", out)
        self.assertIn("①", out)  # circled number
        self.assertIn("②", out)

    def test_multi_json_account_no_raw_json(self):
        values = [
            json.dumps({"email": "a@b.com", "password": "pw1", "twofa": "111"}),
            json.dumps({"email": "b@b.com", "password": "pw2", "twofa": "222"}),
        ]
        out = self._fmt(values)
        self.assertNotIn("{", out)
        self.assertNotIn("|", out)
        self.assertIn("a@b.com", out)
        self.assertIn("b@b.com", out)

    def test_empty_returns_empty_string(self):
        self.assertEqual(self._fmt([]), "")


# ─────────────────────────────────────────────────────────────────────────────
# 5. build_account_delivery_file — JSON format support
# ─────────────────────────────────────────────────────────────────────────────

class BuildAccountDeliveryFileJsonTest(unittest.TestCase):
    def _build(self, assets):
        from services.inventory_import import build_account_delivery_file
        return build_account_delivery_file(
            receipt_number="ORD-20260727-100001",
            product_name="Test Product",
            quantity=len(assets),
            assets=assets,
        )

    def test_json_assets_no_raw_json_in_file(self):
        raw = json.dumps({"email": "a@b.com", "password": "pw", "twofa": "123"})
        content = self._build([raw])
        self.assertNotIn("{", content)
        self.assertNotIn("|", content)
        self.assertIn("a@b.com", content)

    def test_pipe_assets_no_pipe_in_file(self):
        content = self._build(["a@b.com|pw|totp"])
        self.assertNotIn("|", content)
        self.assertIn("a@b.com", content)

    def test_file_is_utf8_compatible(self):
        raw = json.dumps({"email": "用户@示例.com", "password": "密码123"})
        content = self._build([raw])
        # Must encode to UTF-8 without error
        encoded = content.encode("utf-8")
        self.assertIsInstance(encoded, bytes)

    def test_file_contains_order_id(self):
        content = self._build(["a@b.com|pw"])
        self.assertIn("ORD-20260727-100001", content)

    def test_file_multiple_accounts_numbered(self):
        assets = ["a@b.com|pw1", "b@b.com|pw2"]
        content = self._build(assets)
        # Should have two distinct account blocks with numbers
        self.assertIn("a@b.com", content)
        self.assertIn("b@b.com", content)


# ─────────────────────────────────────────────────────────────────────────────
# 6. render_template — blank-line suppression (missing field)
# ─────────────────────────────────────────────────────────────────────────────

class RenderTemplateBlankLineTest(unittest.TestCase):
    def test_missing_twofa_line_dropped(self):
        from services.delivery_message_renderer import render_template
        tmpl = "Email: {email}\n🔐 2FA: {twofa}"
        out = render_template(tmpl, {"email": "a@b.com", "twofa": ""})
        self.assertIn("Email: a@b.com", out)
        self.assertNotIn("🔐 2FA:", out)

    def test_missing_recovery_line_dropped(self):
        from services.delivery_message_renderer import render_template
        tmpl = "Email: {email}\nPassword: {password}\nRecovery: {recovery}"
        out = render_template(tmpl, {"email": "a@b.com", "password": "pw", "recovery": ""})
        self.assertNotIn("Recovery:", out)

    def test_separator_lines_always_kept(self):
        from services.delivery_message_renderer import render_template
        tmpl = "Email: {email}\n━━━━━━━━\nNote: {note}"
        out = render_template(tmpl, {"email": "a@b.com", "note": ""})
        self.assertIn("━━━━━━━━", out)

    def test_no_raw_placeholder_remains(self):
        from services.delivery_message_renderer import render_template
        tmpl = "Hello {name}"
        out = render_template(tmpl, {"name": "World"})
        self.assertEqual(out, "Hello World")


# ─────────────────────────────────────────────────────────────────────────────
# 7. build_delivery_message — accdel_show_* settings
# ─────────────────────────────────────────────────────────────────────────────

class BuildDeliveryMessageShowSettingsTest(unittest.TestCase):
    """accdel_show_* toggles must suppress the corresponding fields at render time."""

    def _build(self, template=None, delivered_asset=None, **show_overrides):
        """Call build_delivery_message with bot_config mocked."""
        from unittest.mock import patch, MagicMock

        defaults = {
            "order_summary": True,
            "product_info": True,
            "purchase_time": True,
            "quantity": True,
            "twofa": True,
        }
        defaults.update(show_overrides)

        fake_show = defaults

        def fake_get_accdel_show():
            return fake_show

        from services import delivery_message_renderer as dmr
        with patch.object(dmr, "_get_accdel_show_settings", fake_get_accdel_show):
            return dmr.build_delivery_message(
                order_id="ORD-TEST-001",
                product_name="Netflix Premium",
                quantity=1,
                amount="$14.99",
                purchase_time="27 Jul 2026 • 14:30 UTC",
                delivered_asset=delivered_asset or "a@b.com|pw|111222",
                template=template,
            )

    def test_all_fields_visible_by_default(self):
        from services.delivery_message_renderer import DEFAULT_TEMPLATE
        out = self._build(template=DEFAULT_TEMPLATE)
        self.assertIn("ORD-TEST-001", out)
        self.assertIn("Netflix Premium", out)

    def test_twofa_hidden_when_toggle_off(self):
        from services.delivery_message_renderer import DEFAULT_TEMPLATE
        out = self._build(template=DEFAULT_TEMPLATE, twofa=False)
        self.assertNotIn("2FA:", out)
        # account email/password still present
        self.assertIn("a@b.com", out)

    def test_quantity_hidden_when_toggle_off(self):
        tmpl = "Qty: {quantity}\nEmail: {email}"
        out = self._build(template=tmpl, quantity=False)
        self.assertNotIn("Qty:", out)

    def test_order_summary_hidden_suppresses_order_id_amount_purchase_time(self):
        tmpl = "Order: {order_id}\nPaid: {amount}\nTime: {purchase_time}\nEmail: {email}"
        out = self._build(template=tmpl, order_summary=False)
        self.assertNotIn("ORD-TEST-001", out)
        self.assertNotIn("$14.99", out)
        self.assertNotIn("27 Jul 2026", out)
        # email still present
        self.assertIn("a@b.com", out)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Backward compatibility — all existing pipe-format tests must still pass
# ─────────────────────────────────────────────────────────────────────────────

class BackwardCompatibilityTest(unittest.TestCase):
    """Verify that none of the audit changes broke pre-existing behaviour."""

    def test_parse_account_inventory_labelled_block(self):
        from services.inventory_import import parse_account_inventory
        text = "Email: user@example.com\nPw: Pass123@\n2fa: abcd efgh ijkl mnop"
        self.assertEqual(
            parse_account_inventory(text),
            ["user@example.com|Pass123@|abcd efgh ijkl mnop"],
        )

    def test_parse_account_inventory_legacy_pipe_unchanged(self):
        from services.inventory_import import parse_account_inventory
        result = parse_account_inventory(
            "a@b.com|pass\nc@d.com|pass2|recovery@x.com|code"
        )
        self.assertEqual(result, ["a@b.com|pass", "c@d.com|pass2|recovery@x.com|code"])

    def test_format_account_delivery_three_field_pipe(self):
        from services.inventory_import import format_account_delivery
        out = format_account_delivery("user@example.com|Pass123@|abcd efgh")
        self.assertEqual(
            out,
            "📧 Email: user@example.com\n🔑 Password: Pass123@\n🔐 2FA: abcd efgh",
        )

    def test_structured_delivery_extract_placeholders(self):
        from services.structured_delivery import extract_placeholders
        template = "Email: {email}\nPassword: {password}\nRecovery: {recovery}\nExpiry: {expiry}"
        self.assertEqual(
            extract_placeholders(template),
            ["email", "password", "recovery", "expiry"],
        )

    def test_structured_delivery_pipe_backward_compat(self):
        from services.structured_delivery import parse_key_value
        raw = "user@example.com|Secr3t!|backup@example.com|2026-12-31"
        fields = parse_key_value(
            raw, placeholders=["email", "password", "recovery", "expiry"]
        )
        self.assertEqual(fields["email"], "user@example.com")
        self.assertEqual(fields["password"], "Secr3t!")
        self.assertEqual(fields["recovery"], "backup@example.com")
        self.assertEqual(fields["expiry"], "2026-12-31")

    def test_structured_delivery_json_backward_compat(self):
        from services.structured_delivery import parse_key_value
        raw = json.dumps({"email": "a@b.com", "password": "pw"})
        fields = parse_key_value(raw)
        self.assertEqual(fields["email"], "a@b.com")
        self.assertEqual(fields["password"], "pw")

    def test_bulk_parse_structured_lines_produces_valid_json(self):
        from services.structured_delivery import bulk_parse_structured_lines
        placeholders = ["email", "password", "recovery", "expiry"]
        text = "a@b.com|pw1|r1@b.com|2026-01-01\nc@d.com|pw2|r2@d.com|2026-02-02"
        lines = bulk_parse_structured_lines(text, placeholders)
        self.assertEqual(len(lines), 2)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["email"], "a@b.com")
        self.assertEqual(parsed["recovery"], "r1@b.com")

    def test_render_preview_no_raw_placeholders(self):
        from services.structured_delivery import render_preview
        TEMPLATE = (
            "📄 Your Account\n"
            "📧 Email: {email}\n"
            "🔑 Password: {password}\n"
            "🔐 Recovery: {recovery}\n"
            "📅 Until: {expiry}"
        )
        rendered = render_preview(TEMPLATE)
        self.assertNotIn("{", rendered)
        self.assertNotIn("}", rendered)


if __name__ == "__main__":
    unittest.main()

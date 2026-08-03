import json

from services.structured_delivery import (
    extract_placeholders,
    render_template,
    parse_key_value,
    render_delivery_message,
    to_storage_value,
    bulk_parse_structured_lines,
    build_sample_fields,
    render_preview,
)

TEMPLATE = (
    "📄 Your Account Details\n"
    "━━━━━━━━━━━━━━\n"
    "📧 Email: {email}\n"
    "🔑 Password: {password}\n"
    "🔐 Recovery Email: {recovery}\n"
    "📅 Valid Until: {expiry}\n"
    "━━━━━━━━━━━━━━\n"
    "⚠️ Please change the password after first login."
)


def test_extract_placeholders_order_and_dedupe():
    assert extract_placeholders(TEMPLATE) == ["email", "password", "recovery", "expiry"]
    assert extract_placeholders("{a}{b}{a}") == ["a", "b"]
    assert extract_placeholders(None) == []
    assert extract_placeholders("") == []


def test_render_template_fills_and_tolerates_missing():
    out = render_template("Hi {name}, code {code}", {"name": "Bob"})
    assert out == "Hi Bob, code "


def test_render_template_empty_template():
    assert render_template("", {"a": "b"}) == ""


def test_parse_key_value_json_structured():
    raw = json.dumps({"email": "a@b.com", "password": "pw"})
    fields = parse_key_value(raw)
    assert fields == {"email": "a@b.com", "password": "pw"}


def test_parse_key_value_legacy_pipe_backward_compat():
    raw = "user@example.com|Secr3t!|backup@example.com|2026-12-31"
    fields = parse_key_value(raw, placeholders=["email", "password", "recovery", "expiry"])
    assert fields["email"] == "user@example.com"
    assert fields["password"] == "Secr3t!"
    assert fields["recovery"] == "backup@example.com"
    assert fields["expiry"] == "2026-12-31"


def test_parse_key_value_plain_single_value():
    fields = parse_key_value("SIMPLE-KEY-1234", placeholders=["key"])
    assert fields["key"] == "SIMPLE-KEY-1234"


def test_render_delivery_message_end_to_end_legacy_pipe():
    raw = "a@b.com|pw123|r@b.com|2027-01-01"
    rendered = render_delivery_message(TEMPLATE, raw)
    assert "📧 Email: a@b.com" in rendered
    assert "🔑 Password: pw123" in rendered
    assert "🔐 Recovery Email: r@b.com" in rendered
    assert "📅 Valid Until: 2027-01-01" in rendered


def test_render_delivery_message_end_to_end_json():
    raw = to_storage_value({"email": "j@x.com", "password": "pw", "recovery": "r@x.com", "expiry": "2027-02-02"})
    rendered = render_delivery_message(TEMPLATE, raw)
    assert "📧 Email: j@x.com" in rendered


def test_bulk_parse_structured_lines_produces_valid_json_per_line():
    placeholders = ["email", "password", "recovery", "expiry"]
    text = "a@b.com|pw1|r1@b.com|2026-01-01\nc@d.com|pw2|r2@d.com|2026-02-02"
    lines = bulk_parse_structured_lines(text, placeholders)
    assert len(lines) == 2
    parsed0 = json.loads(lines[0])
    assert parsed0 == {"email": "a@b.com", "password": "pw1", "recovery": "r1@b.com", "expiry": "2026-01-01"}


def test_bulk_parse_structured_lines_short_line_blanks_trailing_fields():
    placeholders = ["email", "password", "recovery", "expiry"]
    lines = bulk_parse_structured_lines("only@email.com|pw", placeholders)
    parsed = json.loads(lines[0])
    assert parsed["email"] == "only@email.com"
    assert parsed["password"] == "pw"
    assert parsed["recovery"] == ""
    assert parsed["expiry"] == ""


def test_render_preview_uses_plausible_sample_data_no_raw_placeholders_left():
    rendered = render_preview(TEMPLATE)
    assert "{" not in rendered and "}" not in rendered
    assert "@example.com" in rendered  # sample email/recovery


def test_build_sample_fields_unknown_placeholder_gets_generic_sample():
    fields = build_sample_fields(["custom_field"])
    assert fields["custom_field"] == "Sample Custom Field"

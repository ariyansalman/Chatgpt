from services.inventory_import import parse_account_inventory, format_account_delivery


def test_labelled_account_block():
    text = "Email: user@example.com\nPw: Pass123@\n2fa: abcd efgh ijkl mnop"
    assert parse_account_inventory(text) == ["user@example.com|Pass123@|abcd efgh ijkl mnop"]


def test_multiple_labelled_accounts():
    text = "Email: one@example.com\nPw: One123\n2fa: a b c d\n\nEmail: two@example.com\nPassword: Two123\n2fa: e f g h"
    assert parse_account_inventory(text) == ["one@example.com|One123|a b c d", "two@example.com|Two123|e f g h"]


def test_legacy_pipe_format_unchanged():
    assert parse_account_inventory("a@b.com|pass\nc@d.com|pass2|recovery@x.com|code") == ["a@b.com|pass", "c@d.com|pass2|recovery@x.com|code"]


def test_account_delivery_format():
    assert format_account_delivery("user@example.com|Pass123@|abcd efgh") == "📧 Email: user@example.com\n🔑 Password: Pass123@\n🔐 2FA: abcd efgh"

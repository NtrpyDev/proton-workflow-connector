from proton_mail_mcp.redaction import redact_mapping, redact_text


def test_redact_text_removes_secret_values():
    text = "password=secret-value token:abc123 Authentication: Bearer hidden"

    redacted = redact_text(text)

    assert "secret-value" not in redacted
    assert "abc123" not in redacted
    assert "Bearer hidden" not in redacted
    assert "<redacted>" in redacted


def test_redact_mapping_recurses():
    value = {
        "api_key": "abc123",
        "nested": {"Authorization": "Bearer hidden"},
        "safe": "alice@example.com",
    }

    assert redact_mapping(value) == {
        "api_key": "<redacted>",
        "nested": {"Authorization": "<redacted>"},
        "safe": "alice@example.com",
    }

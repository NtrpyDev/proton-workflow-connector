from proton_mail_mcp.config import load_settings


def test_load_settings_from_env_mapping():
    settings = load_settings(
        {
            "PROTON_BRIDGE_IMAP_PORT": "2143",
            "PROTON_BRIDGE_SMTP_PORT": "2025",
            "PROTON_BRIDGE_ALLOW_INSECURE_TLS": "false",
            "PROTON_BRIDGE_USERNAME": "user@example.com",
            "PROTON_BRIDGE_PASSWORD": "fake-password",
            "PROTON_BRIDGE_SENDER_ADDRESSES": "mail@example.com, billing@example.com",
            "SIMPLELOGIN_API_KEY": "fake-simplelogin-key",
            "PROTON_MCP_HTTP_ALLOWED_HOSTS": "mail.example.com,mail.example.com:*",
            "PROTON_MCP_MAX_ATTACHMENTS": "25",
        }
    )

    assert settings.imap_port == 2143
    assert settings.smtp_port == 2025
    assert settings.allow_insecure_tls is False
    assert settings.bridge_email == "user@example.com"
    assert settings.bridge_sender_addresses == ("mail@example.com", "billing@example.com")
    assert settings.http_allowed_hosts == ("mail.example.com", "mail.example.com:*")
    assert settings.max_attachments == 25

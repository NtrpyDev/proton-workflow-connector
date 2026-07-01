import asyncio

import pytest

from proton_mail_mcp.config import Settings
from proton_mail_mcp.server import _require_confirmation, _validate_http_security, build_server


def test_server_registers_complete_tool_surface():
    async def list_names():
        return {tool.name for tool in await build_server().list_tools()}

    names = asyncio.run(list_names())

    assert len(names) == 58
    assert {
        "download_attachment",
        "reply_mail",
        "reply_all",
        "forward_mail",
        "search_all_mail",
        "send_draft",
        "empty_trash",
        "server_status",
    } <= names


def test_destructive_tools_expose_confirmation_field():
    async def schemas():
        return {tool.name: tool.inputSchema for tool in await build_server().list_tools()}

    tool_schemas = asyncio.run(schemas())

    for name in {
        "delete_folder",
        "delete_draft",
        "permanently_delete_message",
        "bulk_permanently_delete",
        "empty_trash",
        "empty_spam",
        "simplelogin_delete_alias",
    }:
        assert "confirm" in tool_schemas[name]["properties"]


def test_confirmation_is_required():
    with pytest.raises(ValueError, match="confirm=true"):
        _require_confirmation(False, "delete")
    _require_confirmation(True, "delete")


def test_nonlocal_http_requires_allowed_hosts_and_authentication():
    with pytest.raises(RuntimeError, match="ALLOWED_HOSTS"):
        _validate_http_security("streamable-http", "0.0.0.0", Settings())
    with pytest.raises(RuntimeError, match="OAuth"):
        _validate_http_security(
            "streamable-http",
            "0.0.0.0",
            Settings(http_allowed_hosts=("mail.example.com:*",)),
        )


def test_hosted_oauth_requires_https():
    settings = Settings(
        http_allowed_hosts=("mail.example.com:*",),
        oauth_issuer_url="https://issuer.example.com",
        oauth_audience="https://mail.example.com/mcp",
        oauth_resource_server_url="http://mail.example.com/mcp",
    )

    with pytest.raises(RuntimeError, match="HTTPS"):
        _validate_http_security("streamable-http", "0.0.0.0", settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"oauth_issuer_url": "http://issuer.example.com"},
        {"oauth_jwks_url": "http://issuer.example.com/jwks"},
    ],
)
def test_hosted_oauth_rejects_insecure_issuer_and_jwks(overrides):
    values = {
        "http_allowed_hosts": ("mail.example.com:*",),
        "oauth_issuer_url": "https://issuer.example.com",
        "oauth_audience": "https://mail.example.com/mcp",
        "oauth_resource_server_url": "https://mail.example.com/mcp",
        **overrides,
    }

    with pytest.raises(RuntimeError, match="HTTPS"):
        _validate_http_security("streamable-http", "0.0.0.0", Settings(**values))


def test_hosted_server_configures_oauth_and_transport_security():
    settings = Settings(
        http_allowed_hosts=("mail.example.com",),
        http_allowed_origins=("https://client.example.com",),
        oauth_issuer_url="https://issuer.example.com",
        oauth_audience="https://mail.example.com/mcp",
        oauth_resource_server_url="https://mail.example.com/mcp",
    )

    server = build_server(settings=settings, host="127.0.0.1", port=8765, enable_http_auth=True)

    assert server.settings.auth.issuer_url.unicode_string() == "https://issuer.example.com/"
    assert server.settings.transport_security.allowed_hosts == ["mail.example.com"]
    assert server.settings.transport_security.allowed_origins == ["https://client.example.com"]

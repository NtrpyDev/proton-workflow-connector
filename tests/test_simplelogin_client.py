from __future__ import annotations

import pytest

from proton_mail_mcp.config import Settings
from proton_mail_mcp.simplelogin_client import SimpleLoginClient, SimpleLoginError


def settings() -> Settings:
    return Settings(simplelogin_api_key="fake-simplelogin-key")


def test_simplelogin_uses_authentication_header_and_params():
    calls = []

    def requester(method, url, headers, params, json_body):
        calls.append((method, url, headers, params, json_body))
        return {"json": {"aliases": [{"email": "alias@example.com"}]}}

    client = SimpleLoginClient(settings(), requester=requester)

    result = client.list_aliases(page_id=1, enabled=True)

    assert result["aliases"][0]["email"] == "alias@example.com"
    method, url, headers, params, json_body = calls[0]
    assert method == "GET"
    assert url == "https://app.simplelogin.io/api/v2/aliases"
    assert headers["Authentication"] == "fake-simplelogin-key"
    assert params == {"page_id": 1, "enabled": "true"}
    assert json_body is None


def test_create_custom_alias_request_shape():
    calls = []

    def requester(method, url, headers, params, json_body):
        calls.append((method, url, headers, params, json_body))
        return {"status_code": 201, "json": {"id": 10, "email": "alias@example.com"}}

    client = SimpleLoginClient(settings(), requester=requester)

    client.create_custom_alias(
        alias_prefix="project",
        signed_suffix="example.com:signature",
        mailbox_ids=[1],
        hostname="example.com",
        note="Synthetic note",
    )

    method, url, _headers, params, json_body = calls[0]
    assert method == "POST"
    assert url.endswith("/api/v3/alias/custom/new")
    assert params == {"hostname": "example.com"}
    assert json_body == {
        "alias_prefix": "project",
        "signed_suffix": "example.com:signature",
        "mailbox_ids": [1],
        "note": "Synthetic note",
    }


def test_alias_filters_are_exclusive():
    client = SimpleLoginClient(settings(), requester=lambda *args: {"json": {}})

    with pytest.raises(ValueError, match="mutually exclusive"):
        client.list_aliases(pinned=True, enabled=True)


def test_simplelogin_errors_include_status_code():
    client = SimpleLoginClient(
        settings(),
        requester=lambda *args: {"status_code": 401, "error": "invalid api key"},
    )

    with pytest.raises(SimpleLoginError, match="401"):
        client.user_info()


def test_all_simplelogin_routes_use_expected_methods_and_paths():
    calls = []

    def requester(method, url, headers, params, json_body):
        calls.append((method, url.removeprefix("https://app.simplelogin.io"), params, json_body))
        return {"json": {"ok": True}}

    client = SimpleLoginClient(settings(), requester=requester)
    client.user_info()
    client.stats()
    client.get_alias(10)
    client.create_random_alias(mode="word")
    client.update_alias(10, note="Updated")
    client.toggle_alias(10)
    client.delete_alias(10)
    client.list_alias_contacts(10, page_id=2)
    client.create_alias_contact(10, contact="alice@example.com")
    client.list_mailboxes()

    assert [(method, path) for method, path, _params, _body in calls] == [
        ("GET", "/api/user_info"),
        ("GET", "/api/stats"),
        ("GET", "/api/aliases/10"),
        ("POST", "/api/alias/random/new"),
        ("PATCH", "/api/aliases/10"),
        ("POST", "/api/aliases/10/toggle"),
        ("DELETE", "/api/aliases/10"),
        ("GET", "/api/aliases/10/contacts"),
        ("POST", "/api/aliases/10/contacts"),
        ("GET", "/api/v2/mailboxes"),
    ]


def test_query_search_uses_post_body():
    calls = []

    def requester(method, url, headers, params, json_body):
        calls.append((method, params, json_body))
        return {"json": {"aliases": []}}

    client = SimpleLoginClient(settings(), requester=requester)
    client.list_aliases(query="project")

    assert calls == [("POST", {"page_id": 0}, {"query": "project"})]


def test_simplelogin_rejects_invalid_updates_and_random_mode():
    client = SimpleLoginClient(settings(), requester=lambda *args: {"json": {}})

    with pytest.raises(ValueError, match="at least one field"):
        client.update_alias(1)
    with pytest.raises(ValueError, match="uuid"):
        client.create_random_alias(mode="invalid")

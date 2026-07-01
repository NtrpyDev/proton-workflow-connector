import json

import pytest
from mcp.server.auth.provider import AccessToken

from proton_mail_mcp.config import Settings
from proton_mail_mcp.security import GuardedClient, OperationGuard, OperationPolicy


class FakeClient:
    def write(self, **kwargs):
        return {"ok": True, **kwargs}


def token(scopes):
    return AccessToken(token="hidden", client_id="client-1", subject="user-1", scopes=scopes)


def test_guard_requires_scope(monkeypatch):
    guard = OperationGuard(Settings(), enforce_auth=True)
    client = GuardedClient(FakeClient(), guard, {"write": OperationPolicy("mail.write", "write")})
    monkeypatch.setattr("proton_mail_mcp.security.get_access_token", lambda: token(["mail.read"]))

    with pytest.raises(PermissionError, match="mail.write"):
        client.write(message_id="1")


def test_guard_rate_limits_authenticated_clients(monkeypatch):
    settings = Settings(rate_limit_write=1)
    guard = OperationGuard(settings, enforce_auth=True)
    client = GuardedClient(FakeClient(), guard, {"write": OperationPolicy("mail.write", "write")})
    monkeypatch.setattr("proton_mail_mcp.security.get_access_token", lambda: token(["mail.write"]))

    assert client.write(message_id="1")["ok"] is True
    with pytest.raises(RuntimeError, match="Rate limit"):
        client.write(message_id="2")


def test_audit_log_excludes_message_content_and_tokens(monkeypatch, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    settings = Settings(audit_log=str(audit_path))
    guard = OperationGuard(settings, enforce_auth=True)
    client = GuardedClient(FakeClient(), guard, {"write": OperationPolicy("mail.write", "write")})
    monkeypatch.setattr("proton_mail_mcp.security.get_access_token", lambda: token(["mail.write"]))

    client.write(message_id="1", text="private body", content_base64="c2VjcmV0")

    record = json.loads(audit_path.read_text())
    assert record["actor"] == "user-1"
    assert record["target"] == {"message_id": "1"}
    assert "private body" not in audit_path.read_text()
    assert "hidden" not in audit_path.read_text()

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


class ModeClient:
    def write(self, **kwargs):
        return {"ok": True}

    def read(self, **kwargs):
        return {"ok": True}

    def send_mail(self, **kwargs):
        return {"ok": True}


def _mode_client(settings, policies):
    return GuardedClient(ModeClient(), OperationGuard(settings, enforce_auth=False), policies)


def test_read_only_blocks_writes_but_allows_reads():
    policies = {"write": OperationPolicy("mail.write", "write"), "read": OperationPolicy("mail.read", "read")}
    client = _mode_client(Settings(read_only=True), policies)
    with pytest.raises(PermissionError, match="read-only"):
        client.write(message_id="1")
    assert client.read()["ok"] is True


def test_read_only_allows_dry_run_preview():
    client = _mode_client(Settings(read_only=True), {"write": OperationPolicy("mail.write", "write")})
    assert client.write(dry_run=True)["ok"] is True


def test_allow_send_false_blocks_send_only():
    policies = {"send_mail": OperationPolicy("mail.write", "write"), "write": OperationPolicy("mail.write", "write")}
    client = _mode_client(Settings(allow_send=False), policies)
    with pytest.raises(PermissionError, match="sending is disabled"):
        client.send_mail(to="x@example.com")
    assert client.write(message_id="1")["ok"] is True  # other writes still allowed


def test_allowed_actions_restricts_by_category():
    policies = {"write": OperationPolicy("mail.write", "write"), "read": OperationPolicy("mail.read", "read")}
    client = _mode_client(Settings(allowed_actions=("read",)), policies)
    assert client.read()["ok"] is True
    with pytest.raises(PermissionError, match="ALLOWED_ACTIONS"):
        client.write(message_id="1")

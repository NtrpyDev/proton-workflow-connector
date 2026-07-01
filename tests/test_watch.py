from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from proton_mail_mcp.config import Settings
from proton_mail_mcp.imap_client import BridgeMailClient
from proton_mail_mcp.watch import (
    CursorStore,
    WatchRule,
    WebhookDeliveryError,
    build_event,
    deliver_webhook,
    poll_rule,
    run_watch,
    sign_payload,
)


def settings(**overrides) -> Settings:
    values = {
        "bridge_username": "user@example.com",
        "bridge_password": "bridge-password",
        "bridge_email": "user@example.com",
        "imap_tls": "none",
        "smtp_tls": "none",
    }
    values.update(overrides)
    return Settings(**values)


def header_bytes(uid: str) -> bytes:
    return (
        f"Subject: Message {uid}\r\n"
        "From: Alice <alice@example.com>\r\n"
        "To: user@example.com\r\n"
        "Date: Tue, 01 Jan 2030 00:00:00 +0000\r\n"
        f"Message-ID: <message-{uid}@example.com>\r\n"
        "\r\n"
    ).encode()


class FakeIMAP:
    instances: list[FakeIMAP] = []
    uidnext = 20
    uidvalidity = 7
    search_uids = b"18 19"

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.commands: list[tuple] = []
        FakeIMAP.instances.append(self)

    def login(self, username: str, password: str):
        return "OK", []

    def logout(self):
        return "OK", []

    def status(self, name, fields):
        self.commands.append(("status", name, fields))
        return "OK", [f"{name} (UIDNEXT {FakeIMAP.uidnext} UIDVALIDITY {FakeIMAP.uidvalidity})".encode()]

    def select(self, folder, readonly: bool = False):
        self.commands.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, command: str, *args):
        self.commands.append(("uid", command, args))
        command = command.upper()
        if command == "SEARCH":
            return "OK", [FakeIMAP.search_uids]
        if command == "FETCH":
            uid = args[0]
            return "OK", [(f"1 (UID {uid} FLAGS (\\Seen))".encode(), header_bytes(uid))]
        raise AssertionError(f"unexpected UID command {command}")


@pytest.fixture(autouse=True)
def reset_fake():
    FakeIMAP.instances.clear()
    FakeIMAP.uidnext = 20
    FakeIMAP.uidvalidity = 7
    FakeIMAP.search_uids = b"18 19"


def test_poll_folder_baselines_on_first_run_without_flooding():
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    result = client.poll_folder(folder="INBOX")

    assert result["baseline"] is True
    assert result["messages"] == []
    assert result["cursor_uid"] == 19  # uidnext - 1
    assert result["uid_validity"] == 7
    # Baseline must not run a SEARCH; it only reads the mailbox head.
    assert not any(cmd[0] == "uid" and cmd[1] == "SEARCH" for cmd in FakeIMAP.instances[0].commands)


def test_poll_folder_returns_only_messages_after_cursor():
    FakeIMAP.search_uids = b"17 18 19"
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    result = client.poll_folder(folder="INBOX", last_uid=17, uid_validity=7)

    uids = [message["uid"] for message in result["messages"]]
    assert uids == ["18", "19"]  # 17 is not strictly greater than the cursor
    assert result["cursor_uid"] == 19
    assert result["baseline"] is False


def test_poll_folder_resets_when_uidvalidity_changes():
    FakeIMAP.uidvalidity = 99
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    result = client.poll_folder(folder="INBOX", last_uid=10, uid_validity=7)

    assert result["reset"] is True
    assert result["messages"] == []
    assert result["cursor_uid"] == 19
    assert result["uid_validity"] == 99


def test_poll_folder_limit_truncates_and_flags_more():
    FakeIMAP.search_uids = b"11 12 13 14"
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    result = client.poll_folder(folder="INBOX", last_uid=10, uid_validity=7, limit=2)

    assert [message["uid"] for message in result["messages"]] == ["11", "12"]
    assert result["cursor_uid"] == 12  # cursor advances only past delivered messages
    assert result["more"] is True


def test_sign_payload_matches_reference_hmac():
    body = b'{"hello":"world"}'
    expected = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert sign_payload("secret", body) == expected


def test_cursor_store_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    store = CursorStore.load(path)
    store.set("INBOX", cursor_uid=42, uid_validity=7)
    store.save()

    reopened = CursorStore.load(path)
    assert reopened.get("INBOX") == (42, 7)
    assert reopened.get("missing") == (0, None)


def test_poll_rule_returns_events_and_cursor_without_persisting(tmp_path):
    FakeIMAP.search_uids = b"18 19"
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)
    store = CursorStore.load(tmp_path / "state.json")
    store.set("inbox", cursor_uid=17, uid_validity=7)
    rule = WatchRule(name="inbox", folder="INBOX")

    outcome = poll_rule(client, rule, store)

    assert [event["type"] for event in outcome["events"]] == ["mail.received", "mail.received"]
    assert outcome["events"][0]["rule"] == "inbox"
    assert outcome["events"][0]["message"]["uid"] == "18"
    assert outcome["cursor_uid"] == 19
    assert outcome["prior_uid"] == 17
    # poll_rule must not persist: the caller commits only after delivery succeeds.
    assert store.get("inbox") == (17, 7)


def test_deliver_webhook_retries_transient_failure_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200) if calls["n"] >= 2 else httpx.Response(503)

    status = deliver_webhook(
        "https://hook.example/ingest",
        build_event("inbox", "INBOX", {"uid": "18"}),
        attempts=3,
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )

    assert status == 200
    assert calls["n"] == 2


def test_deliver_webhook_raises_after_exhausting_retries():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    with pytest.raises(WebhookDeliveryError):
        deliver_webhook(
            "https://hook.example/ingest",
            build_event("inbox", "INBOX", {"uid": "18"}),
            attempts=3,
            transport=httpx.MockTransport(handler),
            sleep=lambda _seconds: None,
        )
    assert calls["n"] == 3


def test_deliver_webhook_does_not_retry_client_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with pytest.raises(WebhookDeliveryError):
        deliver_webhook(
            "https://hook.example/ingest",
            build_event("inbox", "INBOX", {"uid": "18"}),
            attempts=5,
            transport=httpx.MockTransport(handler),
            sleep=lambda _seconds: None,
        )
    assert calls["n"] == 1  # a 4xx is a configuration problem, not worth retrying


def test_run_watch_holds_cursor_when_delivery_fails(tmp_path):
    FakeIMAP.search_uids = b"18 19"
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)
    store = CursorStore.load(tmp_path / "state.json")
    store.set("INBOX", cursor_uid=17, uid_validity=7)
    delivered: list[str] = []

    def failing_sink(event: dict) -> None:
        if event["message"]["uid"] == "19":
            raise RuntimeError("receiver is down")
        delivered.append(event["message"]["uid"])

    count = run_watch(
        settings(watch_webhook_url="https://hook.example/ingest"),
        rules=[WatchRule(name="INBOX", folder="INBOX")],
        client=client,
        store=store,
        once=True,
        sink=failing_sink,
    )

    assert delivered == ["18"]
    assert count == 1
    # 18 was accepted so the cursor advances to 18; 19 failed and is retried next cycle.
    assert store.get("INBOX") == (18, 7)


def test_deliver_webhook_signs_and_posts():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["signature"] = request.headers.get("X-Proton-Signature")
        captured["event_header"] = request.headers.get("X-Proton-Event")
        captured["body"] = request.content
        return httpx.Response(202)

    event = build_event("inbox", "INBOX", {"uid": "18", "subject": "Message 18"})
    status = deliver_webhook(
        "https://hook.example/ingest",
        event,
        secret="shhh",
        transport=httpx.MockTransport(handler),
    )

    assert status == 202
    assert captured["event_header"] == "mail.received"
    expected = sign_payload("shhh", captured["body"])  # type: ignore[arg-type]
    assert captured["signature"] == expected
    assert json.loads(captured["body"])["message"]["uid"] == "18"  # type: ignore[arg-type]


def test_run_watch_once_delivers_new_messages_to_sink(tmp_path):
    FakeIMAP.search_uids = b"18 19"
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)
    store = CursorStore.load(tmp_path / "state.json")
    store.set("INBOX", cursor_uid=17, uid_validity=7)
    collected: list[dict] = []

    count = run_watch(
        settings(watch_webhook_url="https://hook.example/ingest"),
        rules=[WatchRule(name="INBOX", folder="INBOX")],
        client=client,
        store=store,
        once=True,
        sink=collected.append,
    )

    assert count == 2
    assert [event["message"]["uid"] for event in collected] == ["18", "19"]
    assert store.get("INBOX") == (19, 7)

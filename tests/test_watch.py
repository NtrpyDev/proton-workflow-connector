from __future__ import annotations

import hashlib
import hmac
import json
import multiprocessing

import httpx
import pytest

from proton_mail_mcp.config import Settings
from proton_mail_mcp.imap_client import BridgeMailClient
from proton_mail_mcp.watch import (
    CommandDeliveryError,
    CursorStore,
    WatchRule,
    WebhookDeliveryError,
    build_alias_event,
    build_event,
    deliver_webhook,
    load_rules_file,
    make_command_sink,
    make_file_sink,
    poll_rule,
    process_event,
    replay_dead_letter,
    resolve_sink,
    run_watch,
    sign_payload,
)


class RecordingClient:
    """Records action calls (and serves poll_folder) for rule-action tests, no IMAP involved."""

    def __init__(self, messages=None) -> None:
        self.calls: list[tuple] = []
        self.settings = Settings(archive_folder="Archive", trash_folder="Trash")
        self._messages = messages or []

    def poll_folder(self, *, folder, last_uid, uid_validity=None, **criteria):
        msgs = [m for m in self._messages if int(m["uid"]) > last_uid]
        return {
            "folder": folder,
            "messages": msgs,
            "cursor_uid": int(msgs[-1]["uid"]) if msgs else last_uid,
            "uid_validity": 7,
            "baseline": False,
            "reset": False,
            "more": False,
        }

    def mark_read(self, *, message_id, folder):
        self.calls.append(("mark_read", message_id, folder))

    def mark_unread(self, *, message_id, folder):
        self.calls.append(("mark_unread", message_id, folder))

    def star_message(self, *, message_id, folder):
        self.calls.append(("star", message_id, folder))

    def unstar_message(self, *, message_id, folder):
        self.calls.append(("unstar", message_id, folder))

    def apply_label(self, *, message_id, label, folder):
        self.calls.append(("label", message_id, label, folder))

    def remove_label(self, *, message_id, label, folder):
        self.calls.append(("remove_label", message_id, label, folder))

    def archive_message(self, *, message_id, folder):
        self.calls.append(("archive", message_id, folder))

    def trash_message(self, *, message_id, folder):
        self.calls.append(("trash", message_id, folder))

    def move_message(self, *, message_id, destination_folder, folder):
        self.calls.append(("move", message_id, destination_folder, folder))

    def forward_mail(self, *, message_id, to, folder, text):
        self.calls.append(("forward", message_id, to, folder, text))


class FakeSimpleLogin:
    """Minimal stand-in for SimpleLoginClient.poll_aliases in watcher tests."""

    def __init__(self, outcome: dict) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def poll_aliases(self, *, last_id: int = 0, query=None, limit: int = 50, max_pages: int = 20) -> dict:
        self.calls.append({"last_id": last_id, "query": query, "limit": limit})
        return self.outcome


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


def save_cursor_in_process(path: str, name: str, cursor_uid: int, barrier) -> None:
    store = CursorStore.load(path)
    barrier.wait()
    store.set(name, cursor_uid=cursor_uid, uid_validity=7)
    store.save()


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


def test_cursor_store_rejects_corrupt_state_instead_of_baselining(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="(?i)cursor store.*corrupt"):
        CursorStore.load(path)

    assert path.read_text(encoding="utf-8") == "{not-json"


def test_cursor_store_merges_stale_writers(tmp_path):
    path = tmp_path / "state.json"
    first = CursorStore.load(path)
    second = CursorStore.load(path)
    first.set("watcher", cursor_uid=42, uid_validity=7)
    second.set("poll-mailbox", cursor_uid=18, uid_validity=7)

    first.save()
    second.save()

    reopened = CursorStore.load(path)
    assert reopened.get("watcher") == (42, 7)
    assert reopened.get("poll-mailbox") == (18, 7)


def test_cursor_store_does_not_regress_a_concurrently_advanced_cursor(tmp_path):
    path = tmp_path / "state.json"
    stale = CursorStore.load(path)
    advanced = CursorStore.load(path)
    stale.set("shared", cursor_uid=42, uid_validity=7)
    advanced.set("shared", cursor_uid=45, uid_validity=7)

    advanced.save()
    stale.save()

    assert CursorStore.load(path).get("shared") == (45, 7)


def test_cursor_store_serializes_concurrent_process_writers(tmp_path):
    path = tmp_path / "state.json"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    processes = [
        context.Process(target=save_cursor_in_process, args=(str(path), f"cursor-{index}", index, barrier))
        for index in range(1, 5)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    reopened = CursorStore.load(path)
    assert {name: reopened.get(name) for name in (f"cursor-{index}" for index in range(1, 5))} == {
        f"cursor-{index}": (index, 7) for index in range(1, 5)
    }


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


# --- Area 1: SimpleLogin alias event source -------------------------------------------------


def test_build_alias_event_shape():
    event = build_alias_event("aliases", {"id": 7, "email": "a@x.com", "enabled": True, "secret": "x"})

    assert event["type"] == "alias.created"
    assert event["rule"] == "aliases"
    assert event["alias"] == {"id": 7, "email": "a@x.com", "enabled": True}  # only whitelisted fields
    assert "timestamp" in event


def test_poll_rule_dispatches_to_simplelogin_source(tmp_path):
    store = CursorStore.load(tmp_path / "state.json")
    store.set("aliases", cursor_uid=6, uid_validity=None)
    fake = FakeSimpleLogin({"aliases": [{"id": 7, "email": "a@x.com"}], "cursor_id": 7, "baseline": False})
    rule = WatchRule(name="aliases", source="simplelogin_alias", query="a@")

    outcome = poll_rule(None, rule, store, simplelogin=fake)

    assert fake.calls == [{"last_id": 6, "query": "a@", "limit": 50}]
    assert [event["type"] for event in outcome["events"]] == ["alias.created"]
    assert outcome["cursors"] == [7]
    assert outcome["commit_cursor"] == 7
    assert outcome["prior_cursor"] == 6


def test_run_watch_delivers_alias_events(tmp_path):
    store = CursorStore.load(tmp_path / "state.json")
    store.set("aliases", cursor_uid=6, uid_validity=None)
    fake = FakeSimpleLogin(
        {"aliases": [{"id": 7, "email": "a@x.com"}, {"id": 8, "email": "b@x.com"}], "cursor_id": 8, "baseline": False}
    )
    collected: list[dict] = []

    count = run_watch(
        settings(simplelogin_api_key="sl-key"),
        rules=[WatchRule(name="aliases", source="simplelogin_alias")],
        simplelogin=fake,
        store=store,
        once=True,
        sink=collected.append,
    )

    assert count == 2
    assert [event["alias"]["id"] for event in collected] == [7, 8]
    assert store.get("aliases") == (8, None)


# --- Area 2: JSON rules file ----------------------------------------------------------------


def test_load_rules_file_parses_named_triggers(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "invoices",
                        "folder": "INBOX",
                        "from": "billing@x",
                        "unread": True,
                        "webhook_url": "https://a",
                    },
                    {"name": "new-aliases", "source": "simplelogin_alias", "query": "shop"},
                ]
            }
        )
    )

    rules = load_rules_file(path)

    assert [rule.name for rule in rules] == ["invoices", "new-aliases"]
    assert rules[0].source == "mail"
    assert rules[0].from_ == "billing@x"
    assert rules[0].unread is True
    assert rules[0].webhook_url == "https://a"
    assert rules[1].source == "simplelogin_alias"
    assert rules[1].query == "shop"


def test_load_rules_file_rejects_unknown_source(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([{"name": "x", "source": "telegram"}]))
    with pytest.raises(ValueError, match="unknown source"):
        load_rules_file(path)


def test_load_rules_file_rejects_duplicate_names(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([{"name": "dup"}, {"name": "dup"}]))
    with pytest.raises(ValueError, match="Duplicate rule names"):
        load_rules_file(path)


# --- Area 3: Dead-letter / forward progress -------------------------------------------------


def test_dead_letter_advances_after_max_attempts(tmp_path):
    FakeIMAP.search_uids = b"18"
    state_path = tmp_path / "state.json"
    dead_letter = tmp_path / "dead-letter.jsonl"
    st = settings(
        watch_webhook_url="https://hook.example/ingest",
        watch_state_path=str(state_path),
        watch_dead_letter_path=str(dead_letter),
        watch_dead_letter_max_attempts=2,
    )
    rules = [WatchRule(name="INBOX", folder="INBOX")]

    def always_fail(event: dict) -> None:
        raise RuntimeError("receiver is down")

    # Seed the cursor at 17 so UID 18 is the single new event.
    seed = CursorStore.load(state_path)
    seed.set("INBOX", cursor_uid=17, uid_validity=7)
    seed.save()

    # Cycle 1: first failure holds the cursor and writes no dead-letter yet.
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)
    store = CursorStore.load(state_path)
    run_watch(st, rules=rules, client=client, store=store, once=True, sink=always_fail)
    assert store.get("INBOX") == (17, 7)
    assert not dead_letter.exists()

    # Cycle 2: second failing cycle on the same event dead-letters it and advances past it.
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)
    store = CursorStore.load(state_path)
    run_watch(st, rules=rules, client=client, store=store, once=True, sink=always_fail)
    assert store.get("INBOX") == (18, 7)

    lines = dead_letter.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["rule"] == "INBOX"
    assert record["event"]["message"]["uid"] == "18"
    assert record["attempts"] == 2


# --- Area 4: Delivery targets (file + command sinks) ----------------------------------------


def test_file_sink_appends_jsonl(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = make_file_sink(path)

    sink(build_event("inbox", "INBOX", {"uid": "18"}))
    sink(build_event("inbox", "INBOX", {"uid": "19"}))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["message"]["uid"] for line in lines] == ["18", "19"]


def test_run_watch_file_sink_end_to_end(tmp_path):
    FakeIMAP.search_uids = b"18 19"
    out = tmp_path / "events.jsonl"
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)
    store = CursorStore.load(tmp_path / "state.json")
    store.set("INBOX", cursor_uid=17, uid_validity=7)

    count = run_watch(
        settings(watch_sink="file", watch_file_path=str(out)),
        rules=[WatchRule(name="INBOX", folder="INBOX")],
        client=client,
        store=store,
        once=True,
    )

    assert count == 2
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 2
    assert store.get("INBOX") == (19, 7)


def test_command_sink_pipes_event_and_raises_on_failure():
    calls: list[tuple] = []

    def failing_runner(argv, body, timeout):
        calls.append((list(argv), body))
        return 1, "boom"

    sink = make_command_sink("deliver --flag", runner=failing_runner)
    with pytest.raises(CommandDeliveryError):
        sink(build_event("inbox", "INBOX", {"uid": "18"}))

    assert calls[0][0] == ["deliver", "--flag"]
    assert json.loads(calls[0][1])["message"]["uid"] == "18"


def test_command_sink_success_does_not_raise():
    sink = make_command_sink("cat", runner=lambda argv, body, timeout: (0, ""))
    sink(build_event("inbox", "INBOX", {"uid": "18"}))  # must not raise


def test_resolve_sink_per_rule_webhook_bypasses_default_target(tmp_path):
    # A per-rule webhook_url must win even when the global sink is 'file' with no file path set.
    st = settings(watch_sink="file")
    rule = WatchRule(name="a", webhook_url="https://per-rule/ingest")

    sink = resolve_sink(st, rule)

    assert callable(sink)


def test_resolve_sink_errors_when_webhook_target_missing():
    st = settings(watch_sink="webhook")
    with pytest.raises(RuntimeError, match="No webhook URL"):
        resolve_sink(st, WatchRule(name="a"))


# --- Dead-letter replay ---------------------------------------------------------------------


def _write_dead_letter_file(path, events):
    lines = []
    for event in events:
        lines.append(json.dumps({"rule": event["rule"], "source": "mail", "event": event, "attempts": 5}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_replay_dead_letter_redelivers_and_keeps_failures(tmp_path):
    dl = tmp_path / "dead-letter.jsonl"
    events = [build_event("INBOX", "INBOX", {"uid": "18"}), build_event("INBOX", "INBOX", {"uid": "19"})]
    _write_dead_letter_file(dl, events)

    def sink(event):
        if event["message"]["uid"] == "19":
            raise RuntimeError("still down")

    summary = replay_dead_letter(settings(), path=dl, rules=[WatchRule(name="INBOX")], sink=sink)

    assert summary["total"] == 2
    assert summary["replayed"] == 1
    assert summary["remaining"] == 1
    # Only the still-failing event 19 is left in the file.
    remaining = [json.loads(line) for line in dl.read_text(encoding="utf-8").strip().splitlines()]
    assert [rec["event"]["message"]["uid"] for rec in remaining] == ["19"]


def test_replay_dead_letter_removes_file_when_all_delivered(tmp_path):
    dl = tmp_path / "dead-letter.jsonl"
    _write_dead_letter_file(dl, [build_event("INBOX", "INBOX", {"uid": "18"})])

    summary = replay_dead_letter(settings(), path=dl, rules=[WatchRule(name="INBOX")], sink=lambda event: None)

    assert summary["replayed"] == 1
    assert summary["remaining"] == 0
    assert not dl.exists()  # nothing left, so the file is removed


def test_replay_dead_letter_missing_file_is_noop(tmp_path):
    summary = replay_dead_letter(settings(), path=tmp_path / "nope.jsonl", sink=lambda event: None)
    assert summary == {"path": str(tmp_path / "nope.jsonl"), "total": 0, "replayed": 0, "remaining": 0}


def test_replay_dead_letter_preserves_unparseable_lines(tmp_path):
    dl = tmp_path / "dead-letter.jsonl"
    dl.write_text(
        "not json\n"
        + json.dumps({"rule": "INBOX", "source": "mail", "event": build_event("INBOX", "INBOX", {"uid": "18"})})
        + "\n",
        encoding="utf-8",
    )

    summary = replay_dead_letter(settings(), path=dl, rules=[WatchRule(name="INBOX")], sink=lambda event: None)

    assert summary["replayed"] == 1
    # The valid record delivered and dropped; the unparseable line is kept, not lost.
    assert dl.read_text(encoding="utf-8").strip().splitlines() == ["not json"]


# --- Bucket 3: rule actions -----------------------------------------------------------------


def test_process_event_runs_flags_then_sink_then_moves_then_forward():
    client = RecordingClient()
    rule = WatchRule(
        name="triage",
        source="mail",
        folder="INBOX",
        actions=(
            {"type": "label", "label": "News"},
            {"type": "mark_read"},
            {"type": "archive"},
            {"type": "forward", "to": "ops@example.com"},
        ),
    )
    event = build_event("triage", "INBOX", {"uid": "42"})
    # The sink records into the same list so ordering across actions and delivery is visible.
    process_event(client, rule, event, lambda e: client.calls.append(("sink", e["message"]["uid"])))

    kinds = [c[0] for c in client.calls]
    assert kinds == ["label", "mark_read", "sink", "archive", "forward"]
    # forward runs last, reading from the message's final folder (Archive after the archive action).
    assert client.calls[-1] == ("forward", "42", "ops@example.com", "Archive", "")
    # label/mark act in the original folder before the move.
    assert client.calls[0] == ("label", "42", "News", "INBOX")


def test_process_event_without_actions_just_delivers():
    client = RecordingClient()
    rule = WatchRule(name="plain", source="mail", folder="INBOX")
    delivered = []
    process_event(client, rule, build_event("plain", "INBOX", {"uid": "9"}), delivered.append)
    assert client.calls == []
    assert [e["message"]["uid"] for e in delivered] == ["9"]


def test_load_rules_file_parses_actions(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            [{"name": "triage", "from": "news@x", "actions": [{"type": "label", "label": "News"}, {"type": "archive"}]}]
        )
    )
    rules = load_rules_file(path)
    assert rules[0].actions == ({"type": "label", "label": "News"}, {"type": "archive"})


def test_rules_file_rejects_actions_on_alias_source(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([{"name": "a", "source": "simplelogin_alias", "actions": [{"type": "archive"}]}]))
    with pytest.raises(ValueError, match="only supported for mail"):
        load_rules_file(path)


def test_rules_file_rejects_unknown_and_incomplete_actions(tmp_path):
    p1 = tmp_path / "r1.json"
    p1.write_text(json.dumps([{"name": "a", "actions": [{"type": "permanent_delete"}]}]))
    with pytest.raises(ValueError, match="disallowed action"):
        load_rules_file(p1)
    p2 = tmp_path / "r2.json"
    p2.write_text(json.dumps([{"name": "a", "actions": [{"type": "move"}]}]))
    with pytest.raises(ValueError, match="requires a 'folder'"):
        load_rules_file(p2)
    p3 = tmp_path / "r3.json"
    p3.write_text(json.dumps([{"name": "a", "actions": [{"type": "forward"}]}]))
    with pytest.raises(ValueError, match="requires a 'to'"):
        load_rules_file(p3)


def test_run_watch_action_only_rule_runs_actions_without_delivery(tmp_path):
    client = RecordingClient(messages=[{"uid": "42", "subject": "hi"}])
    store = CursorStore.load(tmp_path / "state.json")
    store.set("triage", cursor_uid=41, uid_validity=7)
    rule = WatchRule(
        name="triage",
        source="mail",
        folder="INBOX",
        actions=({"type": "mark_read"}, {"type": "archive"}),
    )

    # No webhook, no sink override: an action-only rule delivers nothing but still acts.
    count = run_watch(settings(), rules=[rule], client=client, store=store, once=True)

    assert count == 1
    kinds = [c[0] for c in client.calls]
    assert kinds == ["mark_read", "archive"]
    assert store.get("triage") == (42, 7)


@pytest.mark.parametrize(
    "safety_settings, error",
    [
        ({"read_only": True}, "read-only"),
        ({"allowed_actions": ("read",)}, "PROTON_MCP_ALLOWED_ACTIONS"),
    ],
)
def test_run_watch_blocks_write_actions_disallowed_by_safety_modes(tmp_path, caplog, safety_settings, error):
    client = RecordingClient(messages=[{"uid": "42", "subject": "hi"}])
    store = CursorStore.load(tmp_path / "state.json")
    store.set("triage", cursor_uid=41, uid_validity=7)
    rule = WatchRule(
        name="triage",
        source="mail",
        folder="INBOX",
        actions=({"type": "mark_read"},),
    )

    count = run_watch(settings(**safety_settings), rules=[rule], client=client, store=store, once=True)

    assert count == 0
    assert client.calls == []
    assert store.get("triage") == (41, 7)
    assert error in caplog.text


def test_run_watch_blocks_forward_when_sending_is_disabled(tmp_path, caplog):
    client = RecordingClient(messages=[{"uid": "42", "subject": "hi"}])
    store = CursorStore.load(tmp_path / "state.json")
    store.set("forward", cursor_uid=41, uid_validity=7)
    rule = WatchRule(
        name="forward",
        source="mail",
        folder="INBOX",
        actions=({"type": "forward", "to": "ops@example.com"},),
    )

    count = run_watch(settings(allow_send=False), rules=[rule], client=client, store=store, once=True)

    assert count == 0
    assert client.calls == []
    assert store.get("forward") == (41, 7)
    assert "sending is disabled" in caplog.text


def test_run_watch_never_dead_letters_an_action_blocked_by_policy(tmp_path):
    client = RecordingClient(messages=[{"uid": "42", "subject": "hi"}])
    store = CursorStore.load(tmp_path / "state.json")
    store.set("triage", cursor_uid=41, uid_validity=7)
    dead_letter = tmp_path / "dead-letter.jsonl"
    rule = WatchRule(
        name="triage",
        source="mail",
        folder="INBOX",
        actions=({"type": "mark_read"},),
    )

    count = run_watch(
        settings(
            read_only=True,
            watch_dead_letter_path=str(dead_letter),
            watch_dead_letter_max_attempts=1,
        ),
        rules=[rule],
        client=client,
        store=store,
        once=True,
    )

    assert count == 0
    assert client.calls == []
    assert store.get("triage") == (41, 7)
    assert not dead_letter.exists()


def test_run_watch_dry_run_does_not_deliver_act_or_advance(tmp_path, caplog):
    caplog.set_level("INFO", logger="proton_workflow_connector.watch")
    client = RecordingClient(messages=[{"uid": "42", "subject": "hi"}])
    store = CursorStore.load(tmp_path / "state.json")
    store.set("triage", cursor_uid=41, uid_validity=7)
    rule = WatchRule(
        name="triage",
        source="mail",
        folder="INBOX",
        actions=({"type": "mark_read"}, {"type": "archive"}),
    )

    count = run_watch(settings(), rules=[rule], client=client, store=store, dry_run=True)

    assert count == 1
    assert client.calls == []
    assert store.get("triage") == (41, 7)
    assert "would run action mark_read" in caplog.text


def test_rules_file_rejects_move_plus_forward(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps([{"name": "a", "actions": [{"type": "archive"}, {"type": "forward", "to": "x@y.com"}]}]))
    with pytest.raises(ValueError, match="move action"):
        load_rules_file(path)

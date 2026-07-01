from __future__ import annotations

import base64
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest

from proton_mail_mcp.config import Settings
from proton_mail_mcp.imap_client import BridgeMailClient, build_search_criteria


def settings(**overrides) -> Settings:
    values = {
        "bridge_username": "user@example.com",
        "bridge_password": "bridge-password",
        "bridge_email": "user@example.com",
        "imap_tls": "none",
        "smtp_tls": "none",
        "bulk_limit": 2,
    }
    values.update(overrides)
    return Settings(**values)


def message_bytes(subject: str = "Hello") -> bytes:
    return (
        f"From: Alice <alice@example.com>\r\n"
        "To: Bob <bob@example.com>\r\n"
        f"Subject: {subject}\r\n"
        "Date: Tue, 01 Jan 2030 00:00:00 +0000\r\n"
        "Message-ID: <message-1@example.com>\r\n"
        "\r\n"
        "Synthetic body.\r\n"
    ).encode()


def attachment_message_bytes() -> bytes:
    message = EmailMessage()
    message["From"] = "Alice <alice@example.com>"
    message["To"] = "user@example.com"
    message["Subject"] = "Attachment test"
    message["Message-ID"] = "<attachment@example.com>"
    message.set_content("Plain body")
    message.add_attachment(b"file contents", maintype="text", subtype="plain", filename="notes.txt")
    return message.as_bytes(policy=policy.default)


def reply_message_bytes() -> bytes:
    message = EmailMessage()
    message["From"] = "Alice <alice@example.com>"
    message["Reply-To"] = "reply@example.com"
    message["To"] = "user@example.com, team@example.com"
    message["Cc"] = "copy@example.com"
    message["Date"] = "Tue, 01 Jan 2030 00:00:00 +0000"
    message["Subject"] = "Project update"
    message["Message-ID"] = "<reply-source@example.com>"
    message["References"] = "<earlier@example.com>"
    message.set_content("Original body")
    return message.as_bytes(policy=policy.default)


class FakeIMAP:
    instances: list[FakeIMAP] = []
    fail_move = False
    messages: dict[str, bytes] = {}
    search_result = b"101 102"
    subscribe_response = ("OK", [b"subscribed"])
    unsubscribe_response = ("OK", [b"unsubscribed"])

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.commands: list[tuple] = []
        FakeIMAP.instances.append(self)

    def login(self, username: str, password: str):
        self.commands.append(("login", username, password))
        return "OK", []

    def logout(self):
        self.commands.append(("logout",))
        return "OK", []

    def list(self):
        self.commands.append(("list",))
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Archive"']

    def create(self, name):
        self.commands.append(("create", name))
        return "OK", [b"created"]

    def rename(self, name, new_name):
        self.commands.append(("rename", name, new_name))
        return "OK", [b"renamed"]

    def delete(self, name):
        self.commands.append(("delete", name))
        return "OK", [b"deleted"]

    def subscribe(self, name):
        self.commands.append(("subscribe", name))
        return FakeIMAP.subscribe_response

    def unsubscribe(self, name):
        self.commands.append(("unsubscribe", name))
        return FakeIMAP.unsubscribe_response

    def status(self, name, fields):
        self.commands.append(("status", name, fields))
        return "OK", [f"{name} (MESSAGES 12 UNSEEN 3 UIDNEXT 20 UIDVALIDITY 7)".encode()]

    def noop(self):
        self.commands.append(("noop",))
        return "OK", []

    def select(self, folder: str, readonly: bool = False):
        self.commands.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, command: str, *args):
        self.commands.append(("uid", command, args))
        command = command.upper()
        if command == "SEARCH":
            return "OK", [FakeIMAP.search_result]
        if command == "FETCH":
            uid = args[0]
            raw = FakeIMAP.messages.get(uid, message_bytes(f"Message {uid}"))
            return "OK", [(f"1 (UID {uid} FLAGS (\\Seen \\Flagged))".encode(), raw)]
        if command == "STORE":
            return "OK", [b"stored"]
        if command == "EXPUNGE":
            return "OK", [b"expunged"]
        if command == "MOVE":
            if FakeIMAP.fail_move:
                return "NO", [b"move unsupported"]
            return "OK", [b"moved"]
        if command == "COPY":
            return "OK", [b"copied"]
        raise AssertionError(f"unexpected UID command: {command}")

    def append(self, mailbox, flags, date_time, message):
        self.commands.append(("append", mailbox, flags, bool(date_time), message))
        return "OK", [b"APPENDUID 1 999"]

    def expunge(self):
        self.commands.append(("expunge",))
        return "OK", [b"1"]


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.commands: list[tuple] = []
        FakeSMTP.instances.append(self)

    def login(self, username: str, password: str):
        self.commands.append(("login", username, password))

    def send_message(self, message, from_addr: str, to_addrs: list[str]):
        self.commands.append(("send_message", message.as_bytes(policy=policy.default), from_addr, to_addrs))

    def quit(self):
        self.commands.append(("quit",))


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeIMAP.instances.clear()
    FakeSMTP.instances.clear()
    FakeIMAP.fail_move = False
    FakeIMAP.messages = {}
    FakeIMAP.search_result = b"101 102"
    FakeIMAP.subscribe_response = ("OK", [b"subscribed"])
    FakeIMAP.unsubscribe_response = ("OK", [b"unsubscribed"])


def test_build_search_criteria_combines_filters():
    assert build_search_criteria(query="invoice", from_="alice@example.com", unread=True, starred=False) == [
        "TEXT",
        '"invoice"',
        "FROM",
        '"alice@example.com"',
        "UNSEEN",
        "UNFLAGGED",
    ]


def test_build_search_criteria_quotes_spaces_and_special_characters():
    assert build_search_criteria(query='quarter "one" \\ draft', subject="Proton Workflow Connector test") == [
        "TEXT",
        '"quarter \\"one\\" \\\\ draft"',
        "SUBJECT",
        '"Proton Workflow Connector test"',
    ]


def test_search_mail_fetches_latest_summaries():
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    results = client.search_mail(query="invoice", unread=True, limit=1)

    assert results[0]["uid"] == "102"
    assert results[0]["subject"] == "Message 102"
    commands = FakeIMAP.instances[0].commands
    assert ("uid", "SEARCH", (None, "TEXT", '"invoice"', "UNSEEN")) in commands


def test_search_quotes_folder_names_with_spaces():
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    client.search_mail(folder="All Mail", limit=1)

    assert ("select", '"All Mail"', True) in FakeIMAP.instances[0].commands


def test_mark_read_sets_seen_flag():
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    result = client.mark_read(message_id="123")

    assert result["updated"] is True
    assert ("uid", "STORE", ("123", "+FLAGS.SILENT", "(\\Seen)")) in FakeIMAP.instances[0].commands


def test_move_falls_back_to_copy_delete_when_move_unsupported():
    FakeIMAP.fail_move = True
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    result = client.move_message(message_id="123", destination_folder="Archive")

    assert result["method"] == "copy-delete"
    commands = FakeIMAP.instances[0].commands
    assert ("uid", "MOVE", ("123", '"Archive"')) in commands
    assert ("uid", "COPY", ("123", '"Archive"')) in commands
    assert ("uid", "STORE", ("123", "+FLAGS.SILENT", "(\\Deleted)")) in commands
    assert ("uid", "EXPUNGE", ("123",)) in commands
    assert ("expunge",) not in commands


def test_permanent_delete_expunge_targets_only_requested_uid():
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    result = client.permanently_delete_message(message_id="123", folder="Trash")

    assert result == {"permanently_deleted": True, "message_ids": ["123"], "via_folder": "Trash"}
    commands = FakeIMAP.instances[0].commands
    assert ("uid", "STORE", ("123", "+FLAGS.SILENT", "(\\Deleted)")) in commands
    assert ("uid", "EXPUNGE", ("123",)) in commands
    assert ("expunge",) not in commands


def test_permanent_delete_moves_to_trash_before_expunge():
    FakeIMAP.messages["123"] = message_bytes()
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    result = client.permanently_delete_message(message_id="123", folder="INBOX")

    assert result == {
        "permanently_deleted": True,
        "message_ids": ["123"],
        "trash_message_ids": ["102"],
        "via_folder": "Trash",
    }
    commands = [command for instance in FakeIMAP.instances for command in instance.commands]
    assert ("uid", "MOVE", ("123", '"Trash"')) in commands
    assert ("uid", "EXPUNGE", ("102",)) in commands
    assert ("uid", "EXPUNGE", ("123",)) not in commands


def test_update_draft_creates_replacement_before_deleting_original():
    client = BridgeMailClient(settings())
    calls = []

    def create_draft(**kwargs):
        calls.append(("create", kwargs))
        return {"created": True}

    def delete_draft(**kwargs):
        calls.append(("delete", kwargs))
        return {"permanently_deleted": True}

    client.create_draft = create_draft
    client.delete_draft = delete_draft
    client._uid_exists = lambda **kwargs: False

    result = client.update_draft(message_id="123", to="alice@example.com", subject="Updated", text="Body")

    assert [name for name, _ in calls] == ["create", "delete"]
    assert result["updated"] is True


def test_bulk_limit_is_enforced():
    client = BridgeMailClient(settings(bulk_limit=2), imap_factory=FakeIMAP)

    with pytest.raises(ValueError, match="Bulk operation exceeds limit"):
        client.bulk_mark_read(message_ids=["1", "2", "3"])


def test_send_mail_formats_recipients_and_removes_bcc_header():
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP, smtp_factory=FakeSMTP)

    result = client.send_mail(
        to=["alice@example.com"],
        cc="team@example.com",
        bcc="hidden@example.com",
        subject="Test",
        text="Synthetic body",
    )

    assert result["recipients"] == ["alice@example.com", "team@example.com", "hidden@example.com"]
    sent = FakeSMTP.instances[0].commands[1]
    parsed = BytesParser(policy=policy.default).parsebytes(sent[1])
    assert parsed["To"] == "alice@example.com"
    assert parsed["Cc"] == "team@example.com"
    assert parsed["Bcc"] is None


def test_folder_management_and_status():
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    client.create_folder(name="Folders/Projects")
    client.rename_folder(name="Folders/Projects", new_name="Folders/Current")
    client.subscribe_folder(name="Folders/Current")
    client.unsubscribe_folder(name="Folders/Current")
    result = client.folder_status(name="Folders/Current")
    client.delete_folder(name="Folders/Current")

    assert result == {
        "folder": "Folders/Current",
        "messages": 12,
        "unseen": 3,
        "uidnext": 20,
        "uidvalidity": 7,
    }


def test_folder_subscriptions_are_idempotent():
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)
    FakeIMAP.subscribe_response = ("NO", [b"already subscribed to this mailbox"])
    FakeIMAP.unsubscribe_response = ("NO", [b"not subscribed to this mailbox"])

    subscribed = client.subscribe_folder(name="Folders/Current")
    unsubscribed = client.unsubscribe_folder(name="Folders/Current")

    assert subscribed == {"subscribed": True, "folder": "Folders/Current", "changed": False}
    assert unsubscribed == {"unsubscribed": True, "folder": "Folders/Current", "changed": False}


def test_attachment_inspection_download_and_send():
    FakeIMAP.messages["77"] = attachment_message_bytes()
    client = BridgeMailClient(
        settings(bridge_sender_addresses=("mail@example.com",)),
        imap_factory=FakeIMAP,
        smtp_factory=FakeSMTP,
    )

    metadata = client.inspect_attachments(message_id="77")
    downloaded = client.download_attachment(message_id="77", attachment_index=0)
    result = client.send_mail(
        to="alice@example.com",
        subject="With attachment",
        text="Body",
        from_address="mail@example.com",
        attachments=[
            {
                "filename": "hello.txt",
                "content_type": "text/plain",
                "content_base64": base64.b64encode(b"hello").decode(),
            }
        ],
    )

    assert metadata[0]["filename"] == "notes.txt"
    assert metadata[0]["index"] == 0
    assert base64.b64decode(downloaded["content_base64"]) == b"file contents"
    assert result["sender"] == "mail@example.com"
    sent = BytesParser(policy=policy.default).parsebytes(FakeSMTP.instances[0].commands[1][1])
    attachment = next(sent.iter_attachments())
    assert attachment.get_filename() == "hello.txt"
    assert attachment.get_payload(decode=True) == b"hello"


def test_send_rejects_unconfigured_sender_and_header_injection():
    client = BridgeMailClient(settings(), smtp_factory=FakeSMTP)

    with pytest.raises(ValueError, match="not configured"):
        client.send_mail(to="alice@example.com", subject="Test", text="Body", from_address="other@example.com")
    with pytest.raises(ValueError, match="newline"):
        client.send_mail(to="alice@example.com", subject="Bad\nBcc: hidden@example.com", text="Body")
    with pytest.raises(ValueError, match="Invalid recipient"):
        client.send_mail(to="not-an-address", subject="Test", text="Body")


def test_reply_all_preserves_thread_headers_and_excludes_sender():
    FakeIMAP.messages["55"] = reply_message_bytes()
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP, smtp_factory=FakeSMTP)

    result = client.reply_mail(message_id="55", text="My reply", reply_all=True)

    assert result["recipients"] == ["reply@example.com", "team@example.com", "copy@example.com"]
    sent = BytesParser(policy=policy.default).parsebytes(FakeSMTP.instances[0].commands[1][1])
    assert sent["Subject"] == "Re: Project update"
    assert sent["In-Reply-To"] == "<reply-source@example.com>"
    assert sent["References"] == "<earlier@example.com> <reply-source@example.com>"
    assert "Original body" in sent.get_body(preferencelist=("plain",)).get_content()


def test_forward_includes_original_attachment():
    FakeIMAP.messages["77"] = attachment_message_bytes()
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP, smtp_factory=FakeSMTP)

    client.forward_mail(message_id="77", to="bob@example.com", text="See attached")

    sent = BytesParser(policy=policy.default).parsebytes(FakeSMTP.instances[0].commands[1][1])
    assert sent["Subject"] == "Fwd: Attachment test"
    assert next(sent.iter_attachments()).get_filename() == "notes.txt"


def test_search_all_mail_deduplicates_message_ids():
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)

    result = client.search_all_mail(query="Message", limit=10)

    assert len(result["messages"]) == 1
    assert result["folders_searched"] == ["INBOX", "Archive"]


def test_search_all_mail_validates_folders_and_global_limit():
    client = BridgeMailClient(settings(search_all_limit=5), imap_factory=FakeIMAP)

    with pytest.raises(ValueError, match="Unknown"):
        client.search_all_mail(folders=["Missing"], limit=5)
    with pytest.raises(ValueError, match="between 0 and 5"):
        client.search_all_mail(limit=6)


def test_bulk_actions_and_empty_folder_use_explicit_uids():
    client = BridgeMailClient(settings(bulk_limit=3), imap_factory=FakeIMAP)

    client.bulk_star(message_ids=["1", "2"])
    copied = client.bulk_copy(message_ids=["1", "2"], destination_folder="Archive")
    deleted = client.bulk_permanently_delete(message_ids=["1", "2"], folder="Trash")
    emptied = client.empty_folder(folder="Trash")

    assert copied["count"] == 2
    assert deleted["count"] == 2
    assert emptied["count"] == 2
    commands = [command for instance in FakeIMAP.instances for command in instance.commands]
    assert ("uid", "EXPUNGE", ("1,2",)) in commands


def test_empty_trash_batches_expunges_at_bulk_limit():
    FakeIMAP.search_result = b"1 2 3 4 5"
    client = BridgeMailClient(settings(bulk_limit=2), imap_factory=FakeIMAP)

    result = client.empty_folder(folder="Trash")

    assert result["count"] == 5
    commands = [command for instance in FakeIMAP.instances for command in instance.commands]
    assert ("uid", "EXPUNGE", ("1,2",)) in commands
    assert ("uid", "EXPUNGE", ("3,4",)) in commands
    assert ("uid", "EXPUNGE", ("5",)) in commands


def test_empty_spam_moves_each_message_through_trash():
    FakeIMAP.search_result = b"1 2"
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP)
    calls = []
    client.permanently_delete_message = lambda **kwargs: calls.append(kwargs)

    result = client.empty_folder(folder="Spam")

    assert result["count"] == 2
    assert calls == [
        {"message_id": "1", "folder": "Spam"},
        {"message_id": "2", "folder": "Spam"},
    ]


def test_bulk_permanent_delete_moves_non_trash_messages_individually():
    client = BridgeMailClient(settings())
    calls = []
    client.permanently_delete_message = lambda **kwargs: calls.append(kwargs)

    result = client.bulk_permanently_delete(message_ids=["1", "2"], folder="INBOX")

    assert result["count"] == 2
    assert calls == [
        {"message_id": "1", "folder": "INBOX"},
        {"message_id": "2", "folder": "INBOX"},
    ]


def test_send_draft_sends_then_deletes_exact_uid():
    draft = EmailMessage()
    draft["From"] = "user@example.com"
    draft["To"] = "alice@example.com"
    draft["Subject"] = "Draft"
    draft["Message-ID"] = "<draft@example.com>"
    draft.set_content("Draft body")
    FakeIMAP.messages["88"] = draft.as_bytes(policy=policy.default)
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP, smtp_factory=FakeSMTP)

    result = client.send_draft(message_id="88")

    assert result["sent"] is True
    assert result["draft_deleted"] is True
    commands = [command for instance in FakeIMAP.instances for command in instance.commands]
    assert ("uid", "MOVE", ("88", '"Trash"')) in commands
    assert ("uid", "EXPUNGE", ("102",)) in commands


def test_send_draft_accepts_draft_already_removed_after_smtp():
    draft = EmailMessage()
    draft["From"] = "user@example.com"
    draft["To"] = "alice@example.com"
    draft["Subject"] = "Draft"
    draft.set_content("Draft body")
    FakeIMAP.messages["88"] = draft.as_bytes(policy=policy.default)
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP, smtp_factory=FakeSMTP)
    client.delete_draft = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("already removed"))
    client._uid_exists = lambda **kwargs: False

    result = client.send_draft(message_id="88")

    assert result["sent"] is True
    assert result["draft_deleted"] is True
    assert result["draft_delete_method"] == "already-removed"


def test_draft_deletion_retries_until_uid_is_absent(monkeypatch):
    client = BridgeMailClient(settings())
    delete_calls = []
    existence_checks = iter([True, False])
    client.delete_draft = lambda **kwargs: delete_calls.append(kwargs)
    client._uid_exists = lambda **kwargs: next(existence_checks)
    monkeypatch.setattr("proton_mail_mcp.imap_client.time.sleep", lambda seconds: None)

    result = client._delete_draft_and_confirm(message_id="88", folder="Drafts")

    assert result == "deleted"
    assert len(delete_calls) == 2


def test_draft_deletion_accepts_bridge_message_does_not_exist():
    client = BridgeMailClient(settings())
    client.delete_draft = lambda **kwargs: (_ for _ in ()).throw(
        RuntimeError("failed to unlabel messages: Message does not exist (Code=2501, Status=0)")
    )
    client._uid_exists = lambda **kwargs: (_ for _ in ()).throw(AssertionError("existence check should not run"))

    result = client._delete_draft_and_confirm(message_id="88", folder="Drafts")

    assert result == "already-removed"


def test_bridge_status_checks_imap_and_smtp():
    client = BridgeMailClient(settings(), imap_factory=FakeIMAP, smtp_factory=FakeSMTP)

    assert client.status() == {"imap": True, "smtp": True}


def test_bridge_connections_use_request_timeout(monkeypatch):
    captured = {}

    def imap_factory(host, port, **kwargs):
        captured["imap"] = (host, port, kwargs)
        return object()

    def smtp_factory(host, port, **kwargs):
        captured["smtp"] = (host, port, kwargs)
        return object()

    monkeypatch.setattr("proton_mail_mcp.imap_client.imaplib.IMAP4", imap_factory)
    monkeypatch.setattr("smtplib.SMTP", smtp_factory)
    client = BridgeMailClient(settings(request_timeout=12.5))

    client._open_imap()
    client._open_smtp()

    assert captured["imap"] == ("127.0.0.1", 1143, {"timeout": 12.5})
    assert captured["smtp"] == ("127.0.0.1", 1025, {"timeout": 12.5})

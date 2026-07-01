from __future__ import annotations

import base64
import html as html_module
import imaplib
import re
import ssl
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any

from .config import Settings
from .mail_models import AttachmentInfo, MessageSummary, build_message, decode_attachments, normalize_recipients
from .redaction import redact_text

UID_RE = re.compile(r"^\d+$")


class BridgeMailClient:
    def __init__(
        self,
        settings: Settings,
        *,
        imap_factory: Callable[[str, int], Any] | None = None,
        smtp_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self._imap_factory = imap_factory
        self._smtp_factory = smtp_factory

    @contextmanager
    def _imap(self) -> Iterator[Any]:
        self.settings.require_bridge()
        conn = self._open_imap()
        try:
            conn.login(self.settings.bridge_username, self.settings.bridge_password)
            yield conn
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _ssl_context(self) -> ssl.SSLContext:
        if self.settings.allow_insecure_tls:
            return ssl._create_unverified_context()
        return ssl.create_default_context()

    def _open_imap(self) -> Any:
        tls = self.settings.imap_tls
        if self._imap_factory is not None:
            conn = self._imap_factory(self.settings.imap_host, self.settings.imap_port)
        elif tls == "ssl":
            conn = imaplib.IMAP4_SSL(
                self.settings.imap_host,
                self.settings.imap_port,
                ssl_context=self._ssl_context(),
                timeout=self.settings.request_timeout,
            )
        else:
            conn = imaplib.IMAP4(
                self.settings.imap_host,
                self.settings.imap_port,
                timeout=self.settings.request_timeout,
            )

        if tls == "starttls":
            try:
                conn.starttls(ssl_context=self._ssl_context())
            except TypeError:
                conn.starttls(self._ssl_context())
        elif tls not in {"none", "ssl"}:
            raise ValueError("PROTON_BRIDGE_IMAP_TLS must be one of: none, starttls, ssl")
        return conn

    def list_folders(self) -> list[dict[str, Any]]:
        with self._imap() as conn:
            status, data = conn.list()
            _require_ok(status, data)
            return [self._parse_folder(line) for line in data if line]

    def create_folder(self, *, name: str) -> dict[str, Any]:
        with self._imap() as conn:
            status, data = conn.create(_mailbox_arg(name))
            _require_ok(status, data)
            return {"created": True, "folder": name}

    def rename_folder(self, *, name: str, new_name: str) -> dict[str, Any]:
        with self._imap() as conn:
            status, data = conn.rename(_mailbox_arg(name), _mailbox_arg(new_name))
            _require_ok(status, data)
            return {"renamed": True, "folder": name, "new_folder": new_name}

    def delete_folder(self, *, name: str) -> dict[str, Any]:
        with self._imap() as conn:
            status, data = conn.delete(_mailbox_arg(name))
            _require_ok(status, data)
            return {"deleted": True, "folder": name}

    def subscribe_folder(self, *, name: str) -> dict[str, Any]:
        with self._imap() as conn:
            status, data = conn.subscribe(_mailbox_arg(name))
            if not _status_ok(status) and _data_contains(data, "already subscribed"):
                return {"subscribed": True, "folder": name, "changed": False}
            _require_ok(status, data)
            return {"subscribed": True, "folder": name, "changed": True}

    def unsubscribe_folder(self, *, name: str) -> dict[str, Any]:
        with self._imap() as conn:
            status, data = conn.unsubscribe(_mailbox_arg(name))
            if not _status_ok(status) and _data_contains(data, "not subscribed"):
                return {"unsubscribed": True, "folder": name, "changed": False}
            _require_ok(status, data)
            return {"unsubscribed": True, "folder": name, "changed": True}

    def folder_status(self, *, name: str) -> dict[str, Any]:
        with self._imap() as conn:
            status, data = conn.status(_mailbox_arg(name), "(MESSAGES UNSEEN UIDNEXT UIDVALIDITY)")
            _require_ok(status, data)
            values = _parse_status(data)
            return {"folder": name, **values}

    def search_mail(
        self,
        *,
        folder: str = "INBOX",
        query: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        since: str | None = None,
        before: str | None = None,
        unread: bool | None = None,
        starred: bool | None = None,
        larger: int | None = None,
        smaller: int | None = None,
        has_attachment: bool | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        criteria = build_search_criteria(
            query=query,
            from_=from_,
            to=to,
            subject=subject,
            since=since,
            before=before,
            unread=unread,
            starred=starred,
            larger=larger,
            smaller=smaller,
            has_attachment=has_attachment,
        )
        with self._imap() as conn:
            self._select(conn, folder, readonly=True)
            status, data = conn.uid("SEARCH", None, *criteria)
            _require_ok(status, data)
            uids = _split_uid_data(data)
            selected = uids[-max(limit, 0) :] if limit else uids
            return [asdict(self._fetch_summary(conn, uid)) for uid in reversed(selected)]

    def search_all_mail(
        self,
        *,
        query: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        since: str | None = None,
        before: str | None = None,
        unread: bool | None = None,
        starred: bool | None = None,
        larger: int | None = None,
        smaller: int | None = None,
        has_attachment: bool | None = None,
        folders: Sequence[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if limit < 0 or limit > self.settings.search_all_limit:
            raise ValueError(f"limit must be between 0 and {self.settings.search_all_limit}")
        available = self.list_folders()
        selectable = [item["name"] for item in available if "\\Noselect" not in item["flags"]]
        targets = list(folders) if folders is not None else selectable
        unknown = [folder for folder in targets if folder not in selectable]
        if unknown:
            raise ValueError(f"Unknown or non-selectable folders: {', '.join(unknown)}")

        messages: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for folder in targets:
            try:
                results = self.search_mail(
                    folder=folder,
                    query=query,
                    from_=from_,
                    to=to,
                    subject=subject,
                    since=since,
                    before=before,
                    unread=unread,
                    starred=starred,
                    larger=larger,
                    smaller=smaller,
                    has_attachment=has_attachment,
                    limit=limit,
                )
                for result in results:
                    result["folder"] = folder
                    messages.append(result)
            except Exception as exc:
                failed.append({"folder": folder, "error": redact_text(str(exc))})

        deduplicated: dict[str, dict[str, Any]] = {}
        for message in messages:
            key = message.get("message_id") or f"{message['folder']}:{message['uid']}"
            deduplicated.setdefault(key, message)
        ordered = sorted(deduplicated.values(), key=_summary_timestamp, reverse=True)
        return {
            "messages": ordered[:limit] if limit else [],
            "folders_searched": targets,
            "folders_failed": failed,
        }

    def poll_folder(
        self,
        *,
        folder: str = "INBOX",
        last_uid: int = 0,
        uid_validity: int | None = None,
        query: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        unread: bool | None = None,
        starred: bool | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return messages that arrived after a stored cursor, for trigger and webhook workflows.

        On the first poll (no cursor) or after an IMAP UIDVALIDITY change, this baselines to the
        current mailbox head and returns no messages, so consumers are never flooded with backlog.
        """
        if limit < 0:
            raise ValueError("limit must be zero or greater")
        criteria = build_search_criteria(
            query=query,
            from_=from_,
            to=to,
            subject=subject,
            unread=unread,
            starred=starred,
        )
        with self._imap() as conn:
            status, data = conn.status(_mailbox_arg(folder), "(UIDNEXT UIDVALIDITY)")
            _require_ok(status, data)
            stats = _parse_status(data)
            current_validity = stats.get("uidvalidity")
            uid_next = stats.get("uidnext", 0)
            head_uid = max(uid_next - 1, 0)

            baseline = uid_validity is None and last_uid == 0
            validity_changed = (
                uid_validity is not None and current_validity is not None and current_validity != uid_validity
            )
            if baseline or validity_changed:
                return {
                    "folder": folder,
                    "messages": [],
                    "cursor_uid": head_uid,
                    "uid_validity": current_validity,
                    "baseline": baseline,
                    "reset": validity_changed,
                }

            self._select(conn, folder, readonly=True)
            status, data = conn.uid("SEARCH", None, "UID", f"{last_uid + 1}:*", *criteria)
            _require_ok(status, data)
            new_uids = sorted((uid for uid in _split_uid_data(data) if int(uid) > last_uid), key=int)
            delivered = new_uids[:limit] if limit else new_uids
            messages = [asdict(self._fetch_summary(conn, uid)) for uid in delivered]
            cursor_uid = int(delivered[-1]) if delivered else last_uid
            return {
                "folder": folder,
                "messages": messages,
                "cursor_uid": cursor_uid,
                "uid_validity": current_validity,
                "baseline": False,
                "reset": False,
                "more": len(new_uids) > len(delivered),
            }

    def read_mail(
        self,
        *,
        message_id: str,
        folder: str = "INBOX",
        mark_seen: bool = False,
        max_body_chars: int | None = None,
    ) -> dict[str, Any]:
        uid, message, flags = self._load_message(message_id=message_id, folder=folder, mark_seen=mark_seen)
        return self._message_to_dict(uid, message, flags, max_body_chars=max_body_chars)

    def read_thread(
        self,
        *,
        message_id: str,
        folder: str = "INBOX",
        limit: int = 20,
        max_body_chars: int | None = None,
    ) -> dict[str, Any]:
        seed = self.read_mail(
            message_id=message_id,
            folder=folder,
            mark_seen=False,
            max_body_chars=max_body_chars,
        )
        header_message_id = seed.get("message_id")
        if not header_message_id:
            return {"messages": [seed], "match": "single-message-no-message-id"}

        with self._imap() as conn:
            self._select(conn, folder, readonly=True)
            status, data = conn.uid(
                "SEARCH",
                None,
                "OR",
                "HEADER",
                "REFERENCES",
                _quote_search_value(header_message_id),
                "HEADER",
                "IN-REPLY-TO",
                _quote_search_value(header_message_id),
            )
            _require_ok(status, data)
            uids = _split_uid_data(data)
            if message_id not in uids:
                uids.insert(0, message_id)
            messages = [
                self.read_mail(message_id=uid, folder=folder, mark_seen=False, max_body_chars=max_body_chars)
                for uid in uids[: max(limit, 1)]
            ]
            return {"messages": messages, "match": "references-or-in-reply-to"}

    def get_headers(self, *, message_id: str, folder: str = "INBOX") -> dict[str, Any]:
        """Return a message's full headers plus parsed authentication, encryption, and unsubscribe info.

        Useful for triage and security checks: ``authentication`` summarizes DMARC/DKIM/SPF results
        and Proton's own markers (origin, at-rest vs end-to-end encryption, spam score), and
        ``list_unsubscribe`` exposes any one-click unsubscribe options.
        """
        uid = _validate_uid(message_id)
        with self._imap() as conn:
            self._select(conn, folder, readonly=True)
            status, data = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER])")
            _require_ok(status, data)
            raw = _extract_fetch_bytes(data)
        message = BytesParser(policy=policy.default).parsebytes(raw)
        headers: dict[str, Any] = {}
        for key in message.keys():
            if key in headers:
                continue
            values = [str(value) for value in message.get_all(key, [])]
            headers[key] = values[0] if len(values) == 1 else values
        return {
            "uid": uid,
            "headers": headers,
            "authentication": _parse_authentication(message),
            "list_unsubscribe": _parse_list_unsubscribe(message),
        }

    def unsubscribe(self, *, message_id: str, folder: str = "INBOX") -> dict[str, Any]:
        """Unsubscribe from a mailing list using the message's ``List-Unsubscribe`` header.

        Prefers RFC 8058 one-click over HTTPS (a POST of ``List-Unsubscribe=One-Click``); otherwise
        falls back to sending the ``mailto:`` unsubscribe from your account. HTTP (non-TLS) one-click
        links are treated as manual and returned rather than fetched. This makes an outbound request
        or email to an address the sender controls, so invoke it deliberately per message.
        """
        _, message, _ = self._load_message(message_id=message_id, folder=folder, mark_seen=False)
        info = _parse_list_unsubscribe(message)
        if not info["present"] or not info["methods"]:
            return {"unsubscribed": False, "method": None, "detail": "No List-Unsubscribe header", "options": info}

        https = next((m for m in info["methods"] if m["target"].lower().startswith("https://")), None)
        mailto = next((m for m in info["methods"] if m["type"] == "mailto"), None)

        if https and info["one_click"]:
            import httpx

            try:
                with httpx.Client(timeout=self.settings.request_timeout, follow_redirects=True) as client:
                    response = client.post(
                        https["target"],
                        data={"List-Unsubscribe": "One-Click"},
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
            except httpx.HTTPError as exc:
                raise RuntimeError(f"One-click unsubscribe request failed: {redact_text(str(exc))}") from exc
            return {
                "unsubscribed": response.status_code < 400,
                "method": "http-one-click",
                "status_code": response.status_code,
                "target": https["target"],
            }
        if mailto:
            address, subject = _parse_mailto(mailto["target"])
            sender = self._resolve_sender(None)
            outgoing = build_message(sender=sender, to=address, subject=subject or "unsubscribe", text="unsubscribe")
            self._send_message(outgoing, sender=sender, recipients=[address])
            return {"unsubscribed": True, "method": "mailto", "target": address, "subject": subject or "unsubscribe"}
        manual = https or info["methods"][0]
        return {
            "unsubscribed": False,
            "method": "manual",
            "target": manual["target"],
            "detail": "No one-click option; open the unsubscribe link manually.",
        }

    def inspect_attachments(self, *, message_id: str, folder: str = "INBOX") -> list[dict[str, Any]]:
        _, message, _ = self._load_message(message_id=message_id, folder=folder, mark_seen=False)
        return [asdict(attachment) for attachment in _attachments(message)]

    def download_attachment(self, *, message_id: str, attachment_index: int, folder: str = "INBOX") -> dict[str, Any]:
        _, message, _ = self._load_message(message_id=message_id, folder=folder, mark_seen=False)
        parts = _attachment_parts(message)
        if attachment_index < 0 or attachment_index >= len(parts):
            raise ValueError(f"attachment_index must be between 0 and {max(len(parts) - 1, 0)}")
        part = parts[attachment_index]
        payload = part.get_payload(decode=True) or b""
        if len(payload) > self.settings.max_attachment_download_bytes:
            raise ValueError(
                f"Attachment size exceeds download limit of {self.settings.max_attachment_download_bytes} bytes"
            )
        return {
            "index": attachment_index,
            "filename": part.get_filename() or f"attachment-{attachment_index}",
            "content_type": part.get_content_type(),
            "size": len(payload),
            "content_id": part.get("Content-ID"),
            "disposition": part.get_content_disposition(),
            "content_base64": base64.b64encode(payload).decode("ascii"),
        }

    def send_mail(
        self,
        *,
        to: str | Iterable[str],
        subject: str,
        text: str,
        cc: str | Iterable[str] | None = None,
        bcc: str | Iterable[str] | None = None,
        html: str | None = None,
        reply_to: str | None = None,
        sender_name: str | None = None,
        from_address: str | None = None,
        attachments: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.settings.require_bridge()
        sender = self._resolve_sender(from_address)
        decoded_attachments = decode_attachments(
            attachments,
            max_count=self.settings.max_attachments,
            max_total_bytes=self.settings.max_outgoing_attachment_bytes,
        )
        message = build_message(
            sender=sender,
            sender_name=sender_name,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            text=text,
            html=html,
            reply_to=reply_to,
            attachments=decoded_attachments,
        )
        recipients = normalize_recipients(to)
        if cc:
            recipients.extend(normalize_recipients(cc))
        if bcc:
            recipients.extend(normalize_recipients(bcc))
            del message["Bcc"]

        self._send_message(message, sender=sender, recipients=recipients)
        return {"sent": True, "sender": sender, "recipients": recipients}

    def _compose_reply(
        self,
        *,
        message_id: str,
        text: str,
        folder: str,
        html: str | None,
        reply_all: bool,
        sender_name: str | None,
        from_address: str | None,
        attachments: Iterable[dict[str, Any]] | None,
    ) -> tuple[Message, str, list[str]]:
        _, original, _ = self._load_message(message_id=message_id, folder=folder, mark_seen=False)
        sender = self._resolve_sender(from_address)
        to, cc = self._reply_recipients(original, sender=sender, reply_all=reply_all)
        original_plain, _ = _extract_body(original, max_chars=self.settings.max_body_chars, content_type="text/plain")
        quoted_text = _quote_reply_text(text, original_plain, _header(original, "Date"), _header(original, "From"))
        quoted_html = None
        if html is not None:
            original_html, _ = _extract_body(original, max_chars=self.settings.max_body_chars, content_type="text/html")
            quoted_html = _quote_reply_html(html, original_html or original_plain, original_is_html=bool(original_html))
        header_message_id = _header(original, "Message-ID")
        references = " ".join(value for value in [_header(original, "References"), header_message_id] if value) or None
        decoded_attachments = decode_attachments(
            attachments,
            max_count=self.settings.max_attachments,
            max_total_bytes=self.settings.max_outgoing_attachment_bytes,
        )
        message = build_message(
            sender=sender,
            sender_name=sender_name,
            to=to,
            cc=cc or None,
            subject=_prefixed_subject(_header(original, "Subject"), "Re:"),
            text=quoted_text,
            html=quoted_html,
            in_reply_to=header_message_id,
            references=references,
            attachments=decoded_attachments,
        )
        return message, sender, [*to, *cc]

    def reply_mail(
        self,
        *,
        message_id: str,
        text: str,
        folder: str = "INBOX",
        html: str | None = None,
        reply_all: bool = False,
        sender_name: str | None = None,
        from_address: str | None = None,
        attachments: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        message, sender, recipients = self._compose_reply(
            message_id=message_id,
            text=text,
            folder=folder,
            html=html,
            reply_all=reply_all,
            sender_name=sender_name,
            from_address=from_address,
            attachments=attachments,
        )
        self._send_message(message, sender=sender, recipients=recipients)
        return {"sent": True, "reply_all": reply_all, "sender": sender, "recipients": recipients}

    def draft_reply(
        self,
        *,
        message_id: str,
        text: str,
        folder: str = "INBOX",
        html: str | None = None,
        reply_all: bool = False,
        sender_name: str | None = None,
        from_address: str | None = None,
        attachments: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compose a reply exactly like ``reply_mail`` but save it to Drafts instead of sending."""
        message, sender, recipients = self._compose_reply(
            message_id=message_id,
            text=text,
            folder=folder,
            html=html,
            reply_all=reply_all,
            sender_name=sender_name,
            from_address=from_address,
            attachments=attachments,
        )
        saved = self._append_draft(message, folder=self.settings.drafts_folder)
        return {
            "drafted": True,
            "reply_all": reply_all,
            "sender": sender,
            "recipients": recipients,
            "folder": saved["folder"],
        }

    def forward_mail(
        self,
        *,
        message_id: str,
        to: str | Iterable[str],
        folder: str = "INBOX",
        text: str = "",
        html: str | None = None,
        cc: str | Iterable[str] | None = None,
        bcc: str | Iterable[str] | None = None,
        sender_name: str | None = None,
        from_address: str | None = None,
        include_original_attachments: bool = True,
        attachments: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        message, sender, recipients = self._compose_forward(
            message_id=message_id,
            to=to,
            folder=folder,
            text=text,
            html=html,
            cc=cc,
            bcc=bcc,
            sender_name=sender_name,
            from_address=from_address,
            include_original_attachments=include_original_attachments,
            attachments=attachments,
        )
        self._send_message(message, sender=sender, recipients=recipients)
        return {"sent": True, "forwarded": True, "sender": sender, "recipients": recipients}

    def draft_forward(
        self,
        *,
        message_id: str,
        to: str | Iterable[str],
        folder: str = "INBOX",
        text: str = "",
        html: str | None = None,
        cc: str | Iterable[str] | None = None,
        bcc: str | Iterable[str] | None = None,
        sender_name: str | None = None,
        from_address: str | None = None,
        include_original_attachments: bool = True,
        attachments: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compose a forward exactly like ``forward_mail`` but save it to Drafts instead of sending."""
        message, sender, recipients = self._compose_forward(
            message_id=message_id,
            to=to,
            folder=folder,
            text=text,
            html=html,
            cc=cc,
            bcc=bcc,
            sender_name=sender_name,
            from_address=from_address,
            include_original_attachments=include_original_attachments,
            attachments=attachments,
        )
        saved = self._append_draft(message, folder=self.settings.drafts_folder)
        return {
            "drafted": True,
            "forwarded": True,
            "sender": sender,
            "recipients": recipients,
            "folder": saved["folder"],
        }

    def _compose_forward(
        self,
        *,
        message_id: str,
        to: str | Iterable[str],
        folder: str,
        text: str,
        html: str | None,
        cc: str | Iterable[str] | None,
        bcc: str | Iterable[str] | None,
        sender_name: str | None,
        from_address: str | None,
        include_original_attachments: bool,
        attachments: Iterable[dict[str, Any]] | None,
    ) -> tuple[Message, str, list[str]]:
        _, original, _ = self._load_message(message_id=message_id, folder=folder, mark_seen=False)
        sender = self._resolve_sender(from_address)
        original_plain, _ = _extract_body(original, max_chars=self.settings.max_body_chars, content_type="text/plain")
        forwarded_text = _forward_text(text, original, original_plain)
        forwarded_html = None
        if html is not None:
            original_html, _ = _extract_body(original, max_chars=self.settings.max_body_chars, content_type="text/html")
            forwarded_html = _forward_html(
                html,
                original,
                original_html or original_plain,
                original_is_html=bool(original_html),
            )
        outgoing = list(attachments or [])
        if include_original_attachments:
            outgoing.extend(_parts_as_attachment_inputs(original))
        decoded_attachments = decode_attachments(
            outgoing,
            max_count=self.settings.max_attachments,
            max_total_bytes=self.settings.max_outgoing_attachment_bytes,
        )
        message = build_message(
            sender=sender,
            sender_name=sender_name,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=_prefixed_subject(_header(original, "Subject"), "Fwd:"),
            text=forwarded_text,
            html=forwarded_html,
            attachments=decoded_attachments,
        )
        recipients = normalize_recipients(to)
        if cc:
            recipients.extend(normalize_recipients(cc))
        if bcc:
            recipients.extend(normalize_recipients(bcc))
            del message["Bcc"]
        return message, sender, recipients

    def create_draft(
        self,
        *,
        to: str | Iterable[str],
        subject: str,
        text: str,
        cc: str | Iterable[str] | None = None,
        bcc: str | Iterable[str] | None = None,
        html: str | None = None,
        folder: str | None = None,
        from_address: str | None = None,
        sender_name: str | None = None,
        attachments: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.settings.require_bridge()
        sender = self._resolve_sender(from_address)
        message = build_message(
            sender=sender,
            sender_name=sender_name,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            text=text,
            html=html,
            attachments=decode_attachments(
                attachments,
                max_count=self.settings.max_attachments,
                max_total_bytes=self.settings.max_outgoing_attachment_bytes,
            ),
        )
        return self._append_draft(message, folder=folder or self.settings.drafts_folder)

    def _append_draft(self, message: Message, *, folder: str) -> dict[str, Any]:
        with self._imap() as conn:
            status, data = conn.append(
                _mailbox_arg(folder), "\\Draft", imaplib.Time2Internaldate(time.time()), message.as_bytes()
            )
            _require_ok(status, data)
            return {"created": True, "folder": folder, "response": _decode_data(data)}

    def update_draft(
        self,
        *,
        message_id: str,
        to: str | Iterable[str],
        subject: str,
        text: str,
        cc: str | Iterable[str] | None = None,
        bcc: str | Iterable[str] | None = None,
        html: str | None = None,
        folder: str | None = None,
        from_address: str | None = None,
        sender_name: str | None = None,
        attachments: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        target = folder or self.settings.drafts_folder
        created = self.create_draft(
            to=to,
            subject=subject,
            text=text,
            cc=cc,
            bcc=bcc,
            html=html,
            folder=target,
            from_address=from_address,
            sender_name=sender_name,
            attachments=attachments,
        )
        self._delete_draft_and_confirm(message_id=message_id, folder=target)
        return {"updated": True, "old_message_id": message_id, "new_draft": created}

    def delete_draft(self, *, message_id: str, folder: str | None = None) -> dict[str, Any]:
        return self.permanently_delete_message(message_id=message_id, folder=folder or self.settings.drafts_folder)

    def send_draft(self, *, message_id: str, folder: str | None = None) -> dict[str, Any]:
        target = folder or self.settings.drafts_folder
        _, message, _ = self._load_message(message_id=message_id, folder=target, mark_seen=False)
        sender = self._resolve_sender(parseaddr(_header(message, "From") or "")[1] or None)
        recipients = _message_recipients(message)
        if not recipients:
            raise ValueError("Draft has no recipients")
        if message.get("Bcc") is not None:
            del message["Bcc"]
        self._send_message(message, sender=sender, recipients=recipients)
        try:
            delete_method = self._delete_draft_and_confirm(message_id=message_id, folder=target)
        except Exception as exc:
            return {
                "sent": True,
                "sender": sender,
                "recipients": recipients,
                "draft_deleted": False,
                "delete_error": redact_text(str(exc)),
            }
        return {
            "sent": True,
            "sender": sender,
            "recipients": recipients,
            "draft_deleted": True,
            "draft_delete_method": delete_method,
        }

    def mark_read(self, *, message_id: str, folder: str = "INBOX") -> dict[str, Any]:
        return self._store_flags(message_id=message_id, folder=folder, operation="+FLAGS.SILENT", flags="(\\Seen)")

    def mark_unread(self, *, message_id: str, folder: str = "INBOX") -> dict[str, Any]:
        return self._store_flags(message_id=message_id, folder=folder, operation="-FLAGS.SILENT", flags="(\\Seen)")

    def star_message(self, *, message_id: str, folder: str = "INBOX") -> dict[str, Any]:
        return self._store_flags(message_id=message_id, folder=folder, operation="+FLAGS.SILENT", flags="(\\Flagged)")

    def unstar_message(self, *, message_id: str, folder: str = "INBOX") -> dict[str, Any]:
        return self._store_flags(message_id=message_id, folder=folder, operation="-FLAGS.SILENT", flags="(\\Flagged)")

    def move_message(self, *, message_id: str, destination_folder: str, folder: str = "INBOX") -> dict[str, Any]:
        uid = _validate_uid(message_id)
        return self._move_uid_set(uid, destination_folder=destination_folder, folder=folder)

    def copy_message(self, *, message_id: str, destination_folder: str, folder: str = "INBOX") -> dict[str, Any]:
        uid = _validate_uid(message_id)
        with self._imap() as conn:
            self._select(conn, folder, readonly=False)
            status, data = conn.uid("COPY", uid, _mailbox_arg(destination_folder))
            _require_ok(status, data)
            return {"copied": True, "message_ids": [uid], "destination_folder": destination_folder}

    def list_labels(self) -> list[dict[str, Any]]:
        """Return Proton labels (selectable mailboxes under the Labels/ prefix).

        Proton labels are additive: a message can carry several, and labelling does not move it out
        of its folder. The built-in ``Starred`` label is managed through ``star_message`` instead.
        """
        prefix = f"{self.settings.labels_folder}/"
        labels = []
        for entry in self.list_folders():
            name = entry["name"]
            if name.startswith(prefix) and "\\Noselect" not in entry["flags"]:
                labels.append({"name": name[len(prefix) :], "mailbox": name, "flags": entry["flags"]})
        return labels

    def apply_label(self, *, message_id: str, label: str, folder: str = "INBOX") -> dict[str, Any]:
        """Add a Proton label to a message by copying it into the label mailbox; it stays in ``folder``."""
        uid = _validate_uid(message_id)
        mailbox = self._label_mailbox(label)
        with self._imap() as conn:
            self._select(conn, folder, readonly=False)
            status, data = conn.uid("COPY", uid, _mailbox_arg(mailbox))
            _require_ok(status, data)
        return {"labeled": True, "message_ids": [uid], "label": mailbox, "folder": folder}

    def remove_label(self, *, message_id: str, label: str, folder: str = "INBOX") -> dict[str, Any]:
        """Remove a Proton label by expunging the message's copy from the label mailbox.

        Expunging from a label mailbox drops the label only; the message stays in its folder. The
        message keeps a different UID inside the label mailbox, so we locate it by ``Message-ID``.
        """
        uid = _validate_uid(message_id)
        mailbox = self._label_mailbox(label)
        _, message, _ = self._load_message(message_id=uid, folder=folder, mark_seen=False)
        header_message_id = _header(message, "Message-ID")
        if not header_message_id:
            raise RuntimeError("Message cannot be unlabeled because it has no Message-ID header")
        with self._imap() as conn:
            self._select(conn, mailbox, readonly=False)
            status, data = conn.uid("SEARCH", None, "HEADER", "MESSAGE-ID", _quote_search_value(header_message_id))
            _require_ok(status, data)
            label_uids = _split_uid_data(data)
            if not label_uids:
                return {"unlabeled": True, "changed": False, "label": mailbox, "message_ids": [uid]}
            uid_set = ",".join(label_uids)
            status, data = conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
            _require_ok(status, data)
            status, data = conn.uid("EXPUNGE", uid_set)
            _require_ok(status, data)
        return {"unlabeled": True, "changed": True, "label": mailbox, "message_ids": [uid]}

    def _label_mailbox(self, label: str) -> str:
        name = label.strip()
        if not name:
            raise ValueError("label must be a non-empty label name")
        prefix = f"{self.settings.labels_folder}/"
        return name if name.startswith(prefix) else f"{prefix}{name}"

    def archive_message(self, *, message_id: str, folder: str = "INBOX") -> dict[str, Any]:
        return self.move_message(message_id=message_id, folder=folder, destination_folder=self.settings.archive_folder)

    def trash_message(self, *, message_id: str, folder: str = "INBOX") -> dict[str, Any]:
        return self.move_message(message_id=message_id, folder=folder, destination_folder=self.settings.trash_folder)

    def mark_spam(self, *, message_id: str, folder: str = "INBOX") -> dict[str, Any]:
        return self.move_message(message_id=message_id, folder=folder, destination_folder=self.settings.spam_folder)

    def mark_not_spam(self, *, message_id: str, destination_folder: str = "INBOX") -> dict[str, Any]:
        return self.move_message(
            message_id=message_id,
            folder=self.settings.spam_folder,
            destination_folder=destination_folder,
        )

    def restore_message(
        self, *, message_id: str, folder: str | None = None, destination_folder: str = "INBOX"
    ) -> dict[str, Any]:
        return self.move_message(
            message_id=message_id,
            folder=folder or self.settings.trash_folder,
            destination_folder=destination_folder,
        )

    def permanently_delete_message(self, *, message_id: str, folder: str = "INBOX") -> dict[str, Any]:
        uid = _validate_uid(message_id)
        if folder.casefold() == self.settings.trash_folder.casefold():
            self._expunge_uid_set(uid, folder=folder)
            return {"permanently_deleted": True, "message_ids": [uid], "via_folder": folder}

        _, message, _ = self._load_message(message_id=uid, folder=folder, mark_seen=False)
        header_message_id = _header(message, "Message-ID")
        if not header_message_id:
            raise RuntimeError("Message cannot be permanently deleted because it has no Message-ID header")
        try:
            self.move_message(message_id=uid, folder=folder, destination_folder=self.settings.trash_folder)
        except RuntimeError as exc:
            if "operation not allowed" in str(exc).casefold():
                raise RuntimeError(
                    f"Folder {folder!r} is read-only in Proton Bridge; delete from a writable folder instead"
                ) from exc
            raise
        trash_uid = self._find_uid_by_message_id(header_message_id, folder=self.settings.trash_folder)
        self._expunge_uid_set(trash_uid, folder=self.settings.trash_folder)
        return {
            "permanently_deleted": True,
            "message_ids": [uid],
            "trash_message_ids": [trash_uid],
            "via_folder": self.settings.trash_folder,
        }

    def bulk_mark_read(self, *, message_ids: Sequence[str], folder: str = "INBOX") -> dict[str, Any]:
        return self._bulk_store(
            message_ids, folder=folder, operation="+FLAGS.SILENT", flags="(\\Seen)", action="marked_read"
        )

    def bulk_mark_unread(self, *, message_ids: Sequence[str], folder: str = "INBOX") -> dict[str, Any]:
        return self._bulk_store(
            message_ids, folder=folder, operation="-FLAGS.SILENT", flags="(\\Seen)", action="marked_unread"
        )

    def bulk_star(self, *, message_ids: Sequence[str], folder: str = "INBOX") -> dict[str, Any]:
        return self._bulk_store(
            message_ids, folder=folder, operation="+FLAGS.SILENT", flags="(\\Flagged)", action="starred"
        )

    def bulk_unstar(self, *, message_ids: Sequence[str], folder: str = "INBOX") -> dict[str, Any]:
        return self._bulk_store(
            message_ids, folder=folder, operation="-FLAGS.SILENT", flags="(\\Flagged)", action="unstarred"
        )

    def bulk_move(
        self, *, message_ids: Sequence[str], destination_folder: str, folder: str = "INBOX"
    ) -> dict[str, Any]:
        uid_set = _validate_uid_list(message_ids, self.settings.bulk_limit)
        return self._move_uid_set(uid_set, destination_folder=destination_folder, folder=folder)

    def bulk_archive(self, *, message_ids: Sequence[str], folder: str = "INBOX") -> dict[str, Any]:
        return self.bulk_move(message_ids=message_ids, folder=folder, destination_folder=self.settings.archive_folder)

    def bulk_trash(self, *, message_ids: Sequence[str], folder: str = "INBOX") -> dict[str, Any]:
        return self.bulk_move(message_ids=message_ids, folder=folder, destination_folder=self.settings.trash_folder)

    def bulk_restore(
        self,
        *,
        message_ids: Sequence[str],
        folder: str | None = None,
        destination_folder: str = "INBOX",
    ) -> dict[str, Any]:
        return self.bulk_move(
            message_ids=message_ids,
            folder=folder or self.settings.trash_folder,
            destination_folder=destination_folder,
        )

    def bulk_copy(
        self, *, message_ids: Sequence[str], destination_folder: str, folder: str = "INBOX"
    ) -> dict[str, Any]:
        uid_set = _validate_uid_list(message_ids, self.settings.bulk_limit)
        with self._imap() as conn:
            self._select(conn, folder, readonly=False)
            status, data = conn.uid("COPY", uid_set, _mailbox_arg(destination_folder))
            _require_ok(status, data)
            return {
                "copied": True,
                "message_ids": uid_set.split(","),
                "count": len(uid_set.split(",")),
                "destination_folder": destination_folder,
            }

    def bulk_permanently_delete(self, *, message_ids: Sequence[str], folder: str = "INBOX") -> dict[str, Any]:
        uid_set = _validate_uid_list(message_ids, self.settings.bulk_limit)
        uids = uid_set.split(",")
        if folder.casefold() == self.settings.trash_folder.casefold():
            self._expunge_uid_set(uid_set, folder=folder)
        else:
            for uid in uids:
                self.permanently_delete_message(message_id=uid, folder=folder)
        return {"permanently_deleted": True, "message_ids": uids, "count": len(uids)}

    def empty_folder(self, *, folder: str) -> dict[str, Any]:
        with self._imap() as conn:
            self._select(conn, folder, readonly=False)
            status, data = conn.uid("SEARCH", None, "ALL")
            _require_ok(status, data)
            uids = _split_uid_data(data)
        if not uids:
            return {"emptied": True, "folder": folder, "count": 0}
        if folder.casefold() != self.settings.trash_folder.casefold():
            for uid in uids:
                self.permanently_delete_message(message_id=uid, folder=folder)
            return {"emptied": True, "folder": folder, "count": len(uids)}
        with self._imap() as conn:
            self._select(conn, folder, readonly=False)
            for start in range(0, len(uids), self.settings.bulk_limit):
                uid_set = ",".join(uids[start : start + self.settings.bulk_limit])
                status, data = conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
                _require_ok(status, data)
                status, data = conn.uid("EXPUNGE", uid_set)
                _require_ok(status, data)
        return {"emptied": True, "folder": folder, "count": len(uids)}

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {"imap": False, "smtp": False}
        try:
            with self._imap() as conn:
                status, _ = conn.noop()
                result["imap"] = _status_ok(status)
        except Exception as exc:
            result["imap_error"] = redact_text(str(exc))
        try:
            smtp = self._open_smtp()
            try:
                smtp.login(self.settings.bridge_username, self.settings.bridge_password)
                result["smtp"] = True
            finally:
                try:
                    smtp.quit()
                except Exception:
                    pass
        except Exception as exc:
            result["smtp_error"] = redact_text(str(exc))
        return result

    def _open_smtp(self) -> Any:
        import smtplib

        tls = self.settings.smtp_tls
        factory = self._smtp_factory
        if factory is not None:
            smtp = factory(self.settings.smtp_host, self.settings.smtp_port)
        elif tls == "ssl":
            smtp = smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                context=self._ssl_context(),
                timeout=self.settings.request_timeout,
            )
        else:
            smtp = smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.request_timeout,
            )

        if tls == "starttls":
            try:
                smtp.starttls(context=self._ssl_context())
            except TypeError:
                smtp.starttls(self._ssl_context())
        elif tls not in {"none", "ssl"}:
            raise ValueError("PROTON_BRIDGE_SMTP_TLS must be one of: none, starttls, ssl")
        return smtp

    def _load_message(self, *, message_id: str, folder: str, mark_seen: bool) -> tuple[str, Message, list[str]]:
        uid = _validate_uid(message_id)
        with self._imap() as conn:
            self._select(conn, folder, readonly=not mark_seen)
            mode = "RFC822" if mark_seen else "BODY.PEEK[]"
            status, data = conn.uid("FETCH", uid, f"({mode} FLAGS)")
            _require_ok(status, data)
            raw = _extract_fetch_bytes(data)
            flags = _extract_flags(data)
            return uid, BytesParser(policy=policy.default).parsebytes(raw), flags

    def _uid_exists(self, *, message_id: str, folder: str) -> bool:
        uid = _validate_uid(message_id)
        with self._imap() as conn:
            self._select(conn, folder, readonly=True)
            status, data = conn.uid("SEARCH", None, "UID", uid)
            _require_ok(status, data)
            return uid in _split_uid_data(data)

    def _delete_draft_and_confirm(self, *, message_id: str, folder: str) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            delete_succeeded = False
            try:
                self.delete_draft(message_id=message_id, folder=folder)
                delete_succeeded = True
            except Exception as exc:
                if _is_missing_message_error(exc):
                    return "already-removed"
                last_error = exc
            try:
                if not self._uid_exists(message_id=message_id, folder=folder):
                    return "deleted" if delete_succeeded else "already-removed"
            except Exception as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
        detail = f": {redact_text(str(last_error))}" if last_error else ""
        raise RuntimeError(f"Draft UID is still present after deletion{detail}")

    def _resolve_sender(self, from_address: str | None) -> str:
        sender = from_address or self.settings.bridge_email
        normalized = parseaddr(sender)[1].lower()
        allowed = {
            parseaddr(value)[1].lower()
            for value in (self.settings.bridge_email, *self.settings.bridge_sender_addresses)
            if parseaddr(value)[1]
        }
        if not normalized or normalized not in allowed:
            raise ValueError(f"Sender address is not configured for this Bridge account: {sender!r}")
        return parseaddr(sender)[1]

    def _send_message(self, message: Message, *, sender: str, recipients: Sequence[str]) -> None:
        smtp = self._open_smtp()
        try:
            smtp.login(self.settings.bridge_username, self.settings.bridge_password)
            smtp.send_message(message, from_addr=sender, to_addrs=list(recipients))
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

    def _reply_recipients(self, message: Message, *, sender: str, reply_all: bool) -> tuple[list[str], list[str]]:
        primary = parseaddr(_header(message, "Reply-To") or _header(message, "From") or "")[1]
        if not primary:
            raise ValueError("Original message does not contain a reply address")
        if not reply_all:
            return [primary], []
        own_addresses = {
            parseaddr(value)[1].lower()
            for value in (sender, self.settings.bridge_email, *self.settings.bridge_sender_addresses)
            if parseaddr(value)[1]
        }
        to_candidates = [primary, *[address for _, address in getaddresses(message.get_all("To", []))]]
        cc_candidates = [address for _, address in getaddresses(message.get_all("Cc", []))]
        to = _unique_addresses(to_candidates, excluded=own_addresses)
        cc = _unique_addresses(cc_candidates, excluded=own_addresses | {item.lower() for item in to})
        return to, cc

    def _select(self, conn: Any, folder: str, *, readonly: bool) -> None:
        status, data = conn.select(_mailbox_arg(folder), readonly=readonly)
        _require_ok(status, data)

    def _find_uid_by_message_id(self, message_id: str, *, folder: str) -> str:
        for attempt in range(10):
            with self._imap() as conn:
                self._select(conn, folder, readonly=True)
                status, data = conn.uid(
                    "SEARCH",
                    None,
                    "HEADER",
                    "MESSAGE-ID",
                    _quote_search_value(message_id),
                )
                _require_ok(status, data)
                uids = _split_uid_data(data)
            if uids:
                return uids[-1]
            if attempt < 9:
                time.sleep(0.5)
        raise RuntimeError("Moved message did not appear in Trash before the deletion timeout")

    def _expunge_uid_set(self, uid_set: str, *, folder: str) -> None:
        with self._imap() as conn:
            self._select(conn, folder, readonly=False)
            status, data = conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
            _require_ok(status, data)
            status, data = conn.uid("EXPUNGE", uid_set)
            _require_ok(status, data)

    def _store_flags(self, *, message_id: str, folder: str, operation: str, flags: str) -> dict[str, Any]:
        uid = _validate_uid(message_id)
        with self._imap() as conn:
            self._select(conn, folder, readonly=False)
            status, data = conn.uid("STORE", uid, operation, flags)
            _require_ok(status, data)
            return {"updated": True, "message_ids": [uid], "operation": operation, "flags": flags}

    def _bulk_store(
        self, message_ids: Sequence[str], *, folder: str, operation: str, flags: str, action: str
    ) -> dict[str, Any]:
        uid_set = _validate_uid_list(message_ids, self.settings.bulk_limit)
        with self._imap() as conn:
            self._select(conn, folder, readonly=False)
            status, data = conn.uid("STORE", uid_set, operation, flags)
            _require_ok(status, data)
            return {action: True, "message_ids": uid_set.split(","), "count": len(uid_set.split(","))}

    def _move_uid_set(self, uid_set: str, *, destination_folder: str, folder: str) -> dict[str, Any]:
        with self._imap() as conn:
            self._select(conn, folder, readonly=False)
            status, data = conn.uid("MOVE", uid_set, _mailbox_arg(destination_folder))
            if _status_ok(status):
                return {
                    "moved": True,
                    "message_ids": uid_set.split(","),
                    "destination_folder": destination_folder,
                    "method": "move",
                }

            status, data = conn.uid("COPY", uid_set, _mailbox_arg(destination_folder))
            _require_ok(status, data)
            status, data = conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
            _require_ok(status, data)
            status, data = conn.uid("EXPUNGE", uid_set)
            _require_ok(status, data)
            return {
                "moved": True,
                "message_ids": uid_set.split(","),
                "destination_folder": destination_folder,
                "method": "copy-delete",
            }

    def _fetch_summary(self, conn: Any, uid: str) -> MessageSummary:
        status, data = conn.uid(
            "FETCH",
            uid,
            "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE MESSAGE-ID)] FLAGS)",
        )
        _require_ok(status, data)
        raw = _extract_fetch_bytes(data)
        message = BytesParser(policy=policy.default).parsebytes(raw)
        flags = _extract_flags(data)
        return MessageSummary(
            uid=uid,
            subject=_header(message, "Subject"),
            from_=_header(message, "From"),
            to=_header(message, "To"),
            date=_header(message, "Date"),
            message_id=_header(message, "Message-ID"),
            flags=flags,
        )

    def _message_to_dict(
        self,
        uid: str,
        message: Message,
        flags: list[str],
        *,
        max_body_chars: int | None,
    ) -> dict[str, Any]:
        max_chars = self.settings.max_body_chars if max_body_chars is None else max_body_chars
        body, body_truncated = _extract_body(message, max_chars=max_chars)
        return {
            "uid": uid,
            "subject": _header(message, "Subject"),
            "from": _header(message, "From"),
            "from_address": parseaddr(_header(message, "From") or "")[1] or None,
            "to": _header(message, "To"),
            "cc": _header(message, "Cc"),
            "date": _header(message, "Date"),
            "message_id": _header(message, "Message-ID"),
            "in_reply_to": _header(message, "In-Reply-To"),
            "references": _header(message, "References"),
            "flags": flags,
            "body": body,
            "body_truncated": body_truncated,
            "attachments": [asdict(attachment) for attachment in _attachments(message)],
        }

    def _parse_folder(self, line: bytes | str) -> dict[str, Any]:
        text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
        flags = re.findall(r"\\[A-Za-z]+", text)
        quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
        name = quoted[-1].replace('\\"', '"') if quoted else text.rsplit(" ", 1)[-1]
        return {"name": name, "flags": flags, "raw": text}


def build_search_criteria(
    *,
    query: str | None = None,
    from_: str | None = None,
    to: str | None = None,
    subject: str | None = None,
    since: str | None = None,
    before: str | None = None,
    unread: bool | None = None,
    starred: bool | None = None,
    larger: int | None = None,
    smaller: int | None = None,
    has_attachment: bool | None = None,
) -> list[str]:
    criteria: list[str] = []
    if query:
        criteria.extend(["TEXT", _quote_search_value(query)])
    if from_:
        criteria.extend(["FROM", _quote_search_value(from_)])
    if to:
        criteria.extend(["TO", _quote_search_value(to)])
    if subject:
        criteria.extend(["SUBJECT", _quote_search_value(subject)])
    if since:
        criteria.extend(["SINCE", since])
    if before:
        criteria.extend(["BEFORE", before])
    if unread is True:
        criteria.append("UNSEEN")
    elif unread is False:
        criteria.append("SEEN")
    if starred is True:
        criteria.append("FLAGGED")
    elif starred is False:
        criteria.append("UNFLAGGED")
    if larger is not None:
        criteria.extend(["LARGER", str(int(larger))])
    if smaller is not None:
        criteria.extend(["SMALLER", str(int(smaller))])
    # IMAP has no "has attachment" key, so approximate with the multipart/mixed content type.
    # This is a best-effort heuristic: it catches most attachments but can miss unusual layouts.
    if has_attachment is True:
        criteria.extend(["HEADER", "Content-Type", _quote_search_value("multipart/mixed")])
    elif has_attachment is False:
        criteria.extend(["NOT", "HEADER", "Content-Type", _quote_search_value("multipart/mixed")])
    return criteria or ["ALL"]


def _quote_search_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _attachments(message: Message) -> list[AttachmentInfo]:
    attachments: list[AttachmentInfo] = []
    for index, part in enumerate(_attachment_parts(message)):
        payload = part.get_payload(decode=True)
        attachments.append(
            AttachmentInfo(
                index=index,
                filename=part.get_filename() or f"attachment-{index}",
                content_type=part.get_content_type(),
                size=len(payload) if payload is not None else None,
                content_id=part.get("Content-ID"),
                disposition=part.get_content_disposition(),
            )
        )
    return attachments


def _attachment_parts(message: Message) -> list[Message]:
    if not message.is_multipart():
        return []
    return [
        part
        for part in message.walk()
        if part.get_content_maintype() != "multipart"
        and (
            part.get_content_disposition() == "attachment"
            or (part.get_content_disposition() == "inline" and (part.get_filename() or part.get("Content-ID")))
        )
    ]


def _parts_as_attachment_inputs(message: Message) -> list[dict[str, str]]:
    output = []
    for index, part in enumerate(_attachment_parts(message)):
        payload = part.get_payload(decode=True) or b""
        item = {
            "filename": part.get_filename() or f"attachment-{index}",
            "content_type": part.get_content_type(),
            "content_base64": base64.b64encode(payload).decode("ascii"),
            "disposition": part.get_content_disposition() or "attachment",
        }
        if part.get("Content-ID"):
            item["content_id"] = part.get("Content-ID")
        output.append(item)
    return output


def _decode_data(data: Any) -> list[str]:
    if not isinstance(data, (list, tuple)):
        data = [data]
    output: list[str] = []
    for item in data:
        if isinstance(item, bytes):
            output.append(item.decode("utf-8", errors="replace"))
        else:
            output.append(str(item))
    return output


def _extract_body(message: Message, *, max_chars: int, content_type: str | None = None) -> tuple[str, bool]:
    if max_chars <= 0:
        return "", False
    candidate: Message | None = None
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            if content_type and part.get_content_type() == content_type:
                candidate = part
                break
            if content_type is None and part.get_content_type() == "text/plain":
                candidate = part
                break
            if content_type is None and candidate is None and part.get_content_type() == "text/html":
                candidate = part
    else:
        if content_type is None or message.get_content_type() == content_type:
            candidate = message
    if candidate is None:
        return "", False
    try:
        body = candidate.get_content()
    except Exception:
        payload = candidate.get_payload(decode=True) or b""
        body = payload.decode(candidate.get_content_charset() or "utf-8", errors="replace")
    truncated = len(body) > max_chars
    return body[:max_chars], truncated


def _extract_fetch_bytes(data: Any) -> bytes:
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    for item in data or []:
        if isinstance(item, bytes) and b"\r\n" in item:
            return item
    raise RuntimeError("IMAP FETCH response did not include message bytes")


def _extract_flags(data: Any) -> list[str]:
    joined = b" ".join(
        part
        for item in data or []
        for part in (item if isinstance(item, tuple) else (item,))
        if isinstance(part, bytes)
    )
    match = re.search(rb"FLAGS \(([^)]*)\)", joined)
    if not match:
        return []
    return [flag.decode("ascii", errors="replace") for flag in match.group(1).split()]


def _header(message: Message, name: str) -> str | None:
    value = message.get(name)
    return str(value) if value is not None else None


def _parse_authentication(message: Message) -> dict[str, Any]:
    """Summarize sender authentication and Proton's own delivery markers from the headers."""
    result: dict[str, Any] = {
        "origin": _header(message, "X-Pm-Origin"),
        "encryption": _header(message, "X-Pm-Content-Encryption"),
        "spam_score": _header(message, "X-Pm-Spamscore"),
        "spam_action": _header(message, "X-Pm-Spam-Action"),
        "dmarc": None,
        "dkim": None,
        "spf": None,
    }
    combined = " ".join(str(value) for value in message.get_all("Authentication-Results", []))
    for key in ("dmarc", "dkim", "spf"):
        match = re.search(rf"\b{key}=(\w+)", combined, flags=re.IGNORECASE)
        if match:
            result[key] = match.group(1).lower()
    return result


def _parse_list_unsubscribe(message: Message) -> dict[str, Any]:
    """Parse the List-Unsubscribe / List-Unsubscribe-Post headers into structured options."""
    raw = _header(message, "List-Unsubscribe")
    if not raw:
        return {"present": False, "one_click": False, "methods": []}
    methods: list[dict[str, str]] = []
    for target in re.findall(r"<([^>]+)>", raw):
        target = target.strip()
        if target.lower().startswith("mailto:"):
            methods.append({"type": "mailto", "target": target})
        elif target.lower().startswith("http"):
            methods.append({"type": "http", "target": target})
    post = _header(message, "List-Unsubscribe-Post") or ""
    return {"present": True, "one_click": "one-click" in post.lower(), "methods": methods}


def _parse_mailto(value: str) -> tuple[str, str | None]:
    """Split a ``mailto:`` unsubscribe target into an address and optional subject."""
    from urllib.parse import parse_qs, unquote

    rest = value[len("mailto:") :] if value.lower().startswith("mailto:") else value
    address, _, query = rest.partition("?")
    subject = parse_qs(query).get("subject", [None])[0]
    return unquote(address), (unquote(subject) if subject else None)


def _mailbox_arg(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _require_ok(status: Any, data: Any) -> None:
    if not _status_ok(status):
        raise RuntimeError(f"IMAP command failed: {status!r} {data!r}")


def _data_contains(data: Any, expected: str) -> bool:
    return expected.casefold() in " ".join(_decode_data(data)).casefold()


def _is_missing_message_error(error: Exception) -> bool:
    text = str(error).casefold()
    return "message does not exist" in text or "no such message" in text


def _split_uid_data(data: Any) -> list[str]:
    chunks: list[bytes] = []
    for item in data or []:
        if isinstance(item, bytes):
            chunks.append(item)
        elif isinstance(item, str):
            chunks.append(item.encode())
    return [
        uid.decode("ascii") for uid in b" ".join(chunks).split() if UID_RE.match(uid.decode("ascii", errors="ignore"))
    ]


def _status_ok(status: Any) -> bool:
    if isinstance(status, bytes):
        status = status.decode("ascii", errors="replace")
    return str(status).upper() == "OK"


def _validate_uid(value: str) -> str:
    uid = str(value).strip()
    if not UID_RE.match(uid):
        raise ValueError("message_id must be a numeric IMAP UID")
    return uid


def _validate_uid_list(message_ids: Sequence[str], limit: int) -> str:
    if not message_ids:
        raise ValueError("message_ids must contain at least one explicit IMAP UID")
    if len(message_ids) > limit:
        raise ValueError(f"Bulk operation exceeds limit of {limit} message IDs")
    return ",".join(_validate_uid(message_id) for message_id in message_ids)


def _parse_status(data: Any) -> dict[str, int]:
    text = " ".join(_decode_data(data))
    values = {}
    for name, value in re.findall(r"(MESSAGES|UNSEEN|UIDNEXT|UIDVALIDITY)\s+(\d+)", text, flags=re.IGNORECASE):
        values[name.lower()] = int(value)
    return values


def _summary_timestamp(message: dict[str, Any]) -> float:
    try:
        value = parsedate_to_datetime(message.get("date") or "")
        return value.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _prefixed_subject(subject: str | None, prefix: str) -> str:
    value = (subject or "").strip()
    if re.match(rf"^{re.escape(prefix)}\s*", value, flags=re.IGNORECASE):
        return value
    return f"{prefix} {value}".rstrip()


def _quote_reply_text(text: str, original: str, date: str | None, sender: str | None) -> str:
    attribution = f"On {date or 'an unknown date'}, {sender or 'the sender'} wrote:"
    quoted = "\n".join(f"> {line}" if line else ">" for line in original.splitlines())
    return f"{text.rstrip()}\n\n{attribution}\n{quoted}".lstrip()


def _quote_reply_html(html: str, original: str, *, original_is_html: bool) -> str:
    content = original if original_is_html else html_module.escape(original).replace("\n", "<br>")
    return f"{html}<br><br><blockquote>{content}</blockquote>"


def _forward_text(text: str, original: Message, body: str) -> str:
    headers = [
        "---------- Forwarded message ----------",
        f"From: {_header(original, 'From') or ''}",
        f"Date: {_header(original, 'Date') or ''}",
        f"Subject: {_header(original, 'Subject') or ''}",
        f"To: {_header(original, 'To') or ''}",
    ]
    return f"{text.rstrip()}\n\n" + "\n".join(headers) + f"\n\n{body}"


def _forward_html(html: str, original: Message, body: str, *, original_is_html: bool) -> str:
    fields = "".join(
        f"<div><strong>{name}:</strong> {html_module.escape(_header(original, name) or '')}</div>"
        for name in ("From", "Date", "Subject", "To")
    )
    original_body = body if original_is_html else html_module.escape(body).replace("\n", "<br>")
    return f"{html}<br><br><div>---------- Forwarded message ----------</div>{fields}<br>{original_body}"


def _message_recipients(message: Message) -> list[str]:
    headers = message.get_all("To", []) + message.get_all("Cc", []) + message.get_all("Bcc", [])
    return _unique_addresses([address for _, address in getaddresses(headers)])


def _unique_addresses(addresses: Iterable[str], *, excluded: set[str] | None = None) -> list[str]:
    blocked = excluded or set()
    seen: set[str] = set()
    output = []
    for address in addresses:
        normalized = parseaddr(address)[1].lower()
        if not normalized or normalized in blocked or normalized in seen:
            continue
        seen.add(normalized)
        output.append(parseaddr(address)[1])
    return output

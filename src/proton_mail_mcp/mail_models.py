from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import PurePath
from typing import Any


@dataclass(frozen=True)
class AttachmentInfo:
    index: int
    filename: str
    content_type: str
    size: int | None
    content_id: str | None = None
    disposition: str | None = None


@dataclass(frozen=True)
class OutgoingAttachment:
    filename: str
    content_type: str
    content: bytes
    disposition: str = "attachment"
    content_id: str | None = None


@dataclass(frozen=True)
class MessageSummary:
    uid: str
    subject: str | None
    from_: str | None
    to: str | None
    date: str | None
    message_id: str | None
    flags: list[str] = field(default_factory=list)


def normalize_recipients(recipients: str | Iterable[str]) -> list[str]:
    if isinstance(recipients, str):
        recipients = [recipients]
    output = []
    for recipient in recipients:
        if not recipient:
            continue
        _require_address(recipient, "recipient")
        output.append(recipient)
    return output


def decode_attachments(
    attachments: Iterable[dict[str, Any]] | None,
    *,
    max_count: int,
    max_total_bytes: int,
) -> list[OutgoingAttachment]:
    if not attachments:
        return []
    items = list(attachments)
    if len(items) > max_count:
        raise ValueError(f"Attachment count exceeds limit of {max_count}")

    decoded: list[OutgoingAttachment] = []
    total = 0
    for item in items:
        filename = str(item.get("filename", "")).strip()
        if not filename or filename != PurePath(filename).name or "/" in filename or "\\" in filename:
            raise ValueError("Attachment filename must be a plain filename without a path")
        _validate_header_value(filename, "attachment filename")
        content_type = str(item.get("content_type") or "application/octet-stream").lower()
        if content_type.count("/") != 1 or any(char.isspace() for char in content_type):
            raise ValueError(f"Invalid attachment content type: {content_type!r}")
        encoded = item.get("content_base64")
        if not isinstance(encoded, str):
            raise ValueError("Attachment content_base64 must be a Base64 string")
        if len(encoded) > ((max_total_bytes + 2) // 3) * 4:
            raise ValueError(f"Total attachment size exceeds limit of {max_total_bytes} bytes")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"Attachment {filename!r} contains invalid Base64 data") from exc
        total += len(content)
        if total > max_total_bytes:
            raise ValueError(f"Total attachment size exceeds limit of {max_total_bytes} bytes")
        disposition = str(item.get("disposition") or "attachment").lower()
        if disposition not in {"attachment", "inline"}:
            raise ValueError("Attachment disposition must be 'attachment' or 'inline'")
        content_id = item.get("content_id")
        if content_id is not None:
            content_id = str(content_id)
            _validate_header_value(content_id, "attachment content ID")
        decoded.append(
            OutgoingAttachment(
                filename=filename,
                content_type=content_type,
                content=content,
                disposition=disposition,
                content_id=content_id,
            )
        )
    return decoded


def build_message(
    *,
    sender: str,
    to: str | Iterable[str],
    subject: str,
    text: str,
    cc: str | Iterable[str] | None = None,
    bcc: str | Iterable[str] | None = None,
    html: str | None = None,
    reply_to: str | None = None,
    sender_name: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: Iterable[OutgoingAttachment] | None = None,
) -> EmailMessage:
    for name, value in {
        "sender": sender,
        "sender name": sender_name,
        "subject": subject,
        "reply-to": reply_to,
        "in-reply-to": in_reply_to,
        "references": references,
    }.items():
        if value is not None:
            _validate_header_value(value, name)
    _require_address(sender, "sender")
    if reply_to:
        _require_address(reply_to, "reply-to")
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    message["To"] = ", ".join(normalize_recipients(to))
    message["Date"] = formatdate(localtime=True)
    sender_domain = sender.rsplit("@", 1)[-1] if "@" in sender else None
    message["Message-ID"] = make_msgid(domain=sender_domain)
    if cc:
        message["Cc"] = ", ".join(normalize_recipients(cc))
    if bcc:
        message["Bcc"] = ", ".join(normalize_recipients(bcc))
    if reply_to:
        message["Reply-To"] = reply_to
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message["Subject"] = subject
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    for attachment in attachments or []:
        maintype, subtype = attachment.content_type.split("/", 1)
        message.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
            disposition=attachment.disposition,
            cid=attachment.content_id,
        )
    return message


def _validate_header_value(value: str, name: str) -> None:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{name} must not contain newline characters")


def _require_address(value: str, name: str) -> str:
    _validate_header_value(value, name)
    address = parseaddr(value)[1]
    if address.count("@") != 1 or any(char.isspace() for char in address):
        raise ValueError(f"Invalid {name} address: {value!r}")
    local, domain = address.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise ValueError(f"Invalid {name} address: {value!r}")
    return address

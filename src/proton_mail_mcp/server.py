from __future__ import annotations

import argparse
from collections.abc import Sequence
from urllib.parse import urlparse

from . import __version__
from .auth import OIDCTokenVerifier
from .config import Settings, load_settings
from .imap_client import BridgeMailClient
from .security import MAIL_POLICIES, SIMPLELOGIN_POLICIES, GuardedClient, OperationGuard
from .simplelogin_client import SimpleLoginClient

SERVER_INSTRUCTIONS = (
    "Use this connector only with the user's locally running Proton Mail Bridge and their own SimpleLogin API key. "
    "Prefer read/search tools before write tools. Permanent deletion, emptying folders, deleting folders, and "
    "deleting aliases are destructive and require confirm=true after explicit user intent. Bulk tools require "
    "explicit IMAP UIDs and are capped by PROTON_MCP_BULK_LIMIT."
)


def build_server(
    *,
    settings: Settings | None = None,
    mail_client: BridgeMailClient | None = None,
    simplelogin_client: SimpleLoginClient | None = None,
    host: str | None = None,
    port: int | None = None,
    enable_http_auth: bool = False,
):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("The 'mcp' package is required. Install this project with `pip install -e .`.") from exc

    settings = settings or load_settings()
    guard = OperationGuard(settings, enforce_auth=enable_http_auth and bool(settings.oauth_issuer_url))
    mail = GuardedClient(mail_client or BridgeMailClient(settings), guard, MAIL_POLICIES)
    simplelogin = GuardedClient(simplelogin_client or SimpleLoginClient(settings), guard, SIMPLELOGIN_POLICIES)
    mcp = _new_fastmcp(
        FastMCP,
        "Proton Workflow Connector",
        settings=settings,
        host=host,
        port=port,
        enable_http_auth=enable_http_auth,
    )

    @mcp.tool()
    def list_folders() -> list[dict]:
        """List folders visible through Proton Mail Bridge IMAP."""
        return mail.list_folders()

    @mcp.tool()
    def folder_status(name: str) -> dict:
        """Get total and unread message counts for one folder."""
        return mail.folder_status(name=name)

    @mcp.tool()
    def create_folder(name: str) -> dict:
        """Create a folder or label using its full Bridge mailbox name."""
        return mail.create_folder(name=name)

    @mcp.tool()
    def rename_folder(name: str, new_name: str) -> dict:
        """Rename one folder or label."""
        return mail.rename_folder(name=name, new_name=new_name)

    @mcp.tool()
    def delete_folder(name: str, confirm: bool = False) -> dict:
        """Permanently delete a folder after explicit confirmation."""
        _require_confirmation(confirm, "delete this folder")
        return mail.delete_folder(name=name)

    @mcp.tool()
    def subscribe_folder(name: str) -> dict:
        """Subscribe to one IMAP folder."""
        return mail.subscribe_folder(name=name)

    @mcp.tool()
    def unsubscribe_folder(name: str) -> dict:
        """Unsubscribe from one IMAP folder."""
        return mail.unsubscribe_folder(name=name)

    @mcp.tool()
    def search_mail(
        folder: str = "INBOX",
        query: str | None = None,
        sender: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        since: str | None = None,
        before: str | None = None,
        unread: bool | None = None,
        starred: bool | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search mail by IMAP criteria and return message summaries with numeric UIDs."""
        return mail.search_mail(
            folder=folder,
            query=query,
            from_=sender,
            to=to,
            subject=subject,
            since=since,
            before=before,
            unread=unread,
            starred=starred,
            limit=limit,
        )

    @mcp.tool()
    def search_all_mail(
        query: str | None = None,
        sender: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        since: str | None = None,
        before: str | None = None,
        unread: bool | None = None,
        starred: bool | None = None,
        folders: list[str] | None = None,
        limit: int = 50,
    ) -> dict:
        """Search all selectable folders and deduplicate results by Message-ID."""
        return mail.search_all_mail(
            query=query,
            from_=sender,
            to=to,
            subject=subject,
            since=since,
            before=before,
            unread=unread,
            starred=starred,
            folders=folders,
            limit=limit,
        )

    @mcp.tool()
    def read_mail(
        message_id: str, folder: str = "INBOX", mark_seen: bool = False, max_body_chars: int | None = None
    ) -> dict:
        """Read one message by numeric IMAP UID."""
        return mail.read_mail(message_id=message_id, folder=folder, mark_seen=mark_seen, max_body_chars=max_body_chars)

    @mcp.tool()
    def read_thread(message_id: str, folder: str = "INBOX", limit: int = 20, max_body_chars: int | None = None) -> dict:
        """Read messages related to one message by Message-ID references."""
        return mail.read_thread(message_id=message_id, folder=folder, limit=limit, max_body_chars=max_body_chars)

    @mcp.tool()
    def inspect_attachments(message_id: str, folder: str = "INBOX") -> list[dict]:
        """List attachment metadata for a message without returning attachment bodies."""
        return mail.inspect_attachments(message_id=message_id, folder=folder)

    @mcp.tool()
    def download_attachment(message_id: str, attachment_index: int, folder: str = "INBOX") -> dict:
        """Return one attachment as Base64 with its filename and MIME metadata."""
        return mail.download_attachment(
            message_id=message_id,
            attachment_index=attachment_index,
            folder=folder,
        )

    @mcp.tool()
    def send_mail(
        to: str | list[str],
        subject: str,
        text: str,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        html: str | None = None,
        reply_to: str | None = None,
        sender_name: str | None = None,
        from_address: str | None = None,
        attachments: list[dict] | None = None,
    ) -> dict:
        """Send mail through Proton Mail Bridge SMTP."""
        return mail.send_mail(
            to=to,
            subject=subject,
            text=text,
            cc=cc,
            bcc=bcc,
            html=html,
            reply_to=reply_to,
            sender_name=sender_name,
            from_address=from_address,
            attachments=attachments,
        )

    @mcp.tool()
    def reply_mail(
        message_id: str,
        text: str,
        folder: str = "INBOX",
        html: str | None = None,
        sender_name: str | None = None,
        from_address: str | None = None,
        attachments: list[dict] | None = None,
    ) -> dict:
        """Reply to the sender while preserving standard email thread headers."""
        return mail.reply_mail(
            message_id=message_id,
            text=text,
            folder=folder,
            html=html,
            reply_all=False,
            sender_name=sender_name,
            from_address=from_address,
            attachments=attachments,
        )

    @mcp.tool()
    def reply_all(
        message_id: str,
        text: str,
        folder: str = "INBOX",
        html: str | None = None,
        sender_name: str | None = None,
        from_address: str | None = None,
        attachments: list[dict] | None = None,
    ) -> dict:
        """Reply to all original recipients except configured sender addresses."""
        return mail.reply_mail(
            message_id=message_id,
            text=text,
            folder=folder,
            html=html,
            reply_all=True,
            sender_name=sender_name,
            from_address=from_address,
            attachments=attachments,
        )

    @mcp.tool()
    def forward_mail(
        message_id: str,
        to: str | list[str],
        folder: str = "INBOX",
        text: str = "",
        html: str | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        sender_name: str | None = None,
        from_address: str | None = None,
        include_original_attachments: bool = True,
        attachments: list[dict] | None = None,
    ) -> dict:
        """Forward one message with optional original and new attachments."""
        return mail.forward_mail(
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

    @mcp.tool()
    def create_draft(
        to: str | list[str],
        subject: str,
        text: str,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        html: str | None = None,
        folder: str | None = None,
        sender_name: str | None = None,
        from_address: str | None = None,
        attachments: list[dict] | None = None,
    ) -> dict:
        """Create a draft message in the configured drafts folder."""
        return mail.create_draft(
            to=to,
            subject=subject,
            text=text,
            cc=cc,
            bcc=bcc,
            html=html,
            folder=folder,
            sender_name=sender_name,
            from_address=from_address,
            attachments=attachments,
        )

    @mcp.tool()
    def update_draft(
        message_id: str,
        to: str | list[str],
        subject: str,
        text: str,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        html: str | None = None,
        folder: str | None = None,
        sender_name: str | None = None,
        from_address: str | None = None,
        attachments: list[dict] | None = None,
    ) -> dict:
        """Replace a draft by creating the new draft before deleting the old UID."""
        return mail.update_draft(
            message_id=message_id,
            to=to,
            subject=subject,
            text=text,
            cc=cc,
            bcc=bcc,
            html=html,
            folder=folder,
            sender_name=sender_name,
            from_address=from_address,
            attachments=attachments,
        )

    @mcp.tool()
    def delete_draft(message_id: str, folder: str | None = None, confirm: bool = False) -> dict:
        """Delete a draft by numeric IMAP UID."""
        _require_confirmation(confirm, "permanently delete this draft")
        return mail.delete_draft(message_id=message_id, folder=folder)

    @mcp.tool()
    def send_draft(message_id: str, folder: str | None = None) -> dict:
        """Send an existing draft and remove it after SMTP accepts the message."""
        return mail.send_draft(message_id=message_id, folder=folder)

    @mcp.tool()
    def mark_read(message_id: str, folder: str = "INBOX") -> dict:
        """Mark one message read."""
        return mail.mark_read(message_id=message_id, folder=folder)

    @mcp.tool()
    def mark_unread(message_id: str, folder: str = "INBOX") -> dict:
        """Mark one message unread."""
        return mail.mark_unread(message_id=message_id, folder=folder)

    @mcp.tool()
    def star_message(message_id: str, folder: str = "INBOX") -> dict:
        """Star one message."""
        return mail.star_message(message_id=message_id, folder=folder)

    @mcp.tool()
    def unstar_message(message_id: str, folder: str = "INBOX") -> dict:
        """Remove the star from one message."""
        return mail.unstar_message(message_id=message_id, folder=folder)

    @mcp.tool()
    def move_message(message_id: str, destination_folder: str, folder: str = "INBOX") -> dict:
        """Move one message to another folder."""
        return mail.move_message(message_id=message_id, destination_folder=destination_folder, folder=folder)

    @mcp.tool()
    def copy_message(message_id: str, destination_folder: str, folder: str = "INBOX") -> dict:
        """Copy one message to another folder."""
        return mail.copy_message(message_id=message_id, destination_folder=destination_folder, folder=folder)

    @mcp.tool()
    def archive_message(message_id: str, folder: str = "INBOX") -> dict:
        """Move one message to the configured archive folder."""
        return mail.archive_message(message_id=message_id, folder=folder)

    @mcp.tool()
    def trash_message(message_id: str, folder: str = "INBOX") -> dict:
        """Move one message to the configured trash folder."""
        return mail.trash_message(message_id=message_id, folder=folder)

    @mcp.tool()
    def mark_spam(message_id: str, folder: str = "INBOX") -> dict:
        """Move one message to the configured Spam folder."""
        return mail.mark_spam(message_id=message_id, folder=folder)

    @mcp.tool()
    def mark_not_spam(message_id: str, destination_folder: str = "INBOX") -> dict:
        """Move one message from Spam to another folder."""
        return mail.mark_not_spam(message_id=message_id, destination_folder=destination_folder)

    @mcp.tool()
    def restore_message(message_id: str, folder: str | None = None, destination_folder: str = "INBOX") -> dict:
        """Restore one message from trash or another folder."""
        return mail.restore_message(message_id=message_id, folder=folder, destination_folder=destination_folder)

    @mcp.tool()
    def permanently_delete_message(message_id: str, folder: str = "INBOX", confirm: bool = False) -> dict:
        """Move one message from a writable folder through Trash, then selectively expunge it."""
        _require_confirmation(confirm, "permanently delete this message")
        return mail.permanently_delete_message(message_id=message_id, folder=folder)

    @mcp.tool()
    def bulk_mark_read(message_ids: list[str], folder: str = "INBOX") -> dict:
        """Mark explicit message UIDs read, capped by PROTON_MCP_BULK_LIMIT."""
        return mail.bulk_mark_read(message_ids=message_ids, folder=folder)

    @mcp.tool()
    def bulk_mark_unread(message_ids: list[str], folder: str = "INBOX") -> dict:
        """Mark explicit message UIDs unread, capped by PROTON_MCP_BULK_LIMIT."""
        return mail.bulk_mark_unread(message_ids=message_ids, folder=folder)

    @mcp.tool()
    def bulk_star(message_ids: list[str], folder: str = "INBOX") -> dict:
        """Star explicit message UIDs, capped by PROTON_MCP_BULK_LIMIT."""
        return mail.bulk_star(message_ids=message_ids, folder=folder)

    @mcp.tool()
    def bulk_unstar(message_ids: list[str], folder: str = "INBOX") -> dict:
        """Unstar explicit message UIDs, capped by PROTON_MCP_BULK_LIMIT."""
        return mail.bulk_unstar(message_ids=message_ids, folder=folder)

    @mcp.tool()
    def bulk_move(message_ids: list[str], destination_folder: str, folder: str = "INBOX") -> dict:
        """Move explicit message UIDs to another folder, capped by PROTON_MCP_BULK_LIMIT."""
        return mail.bulk_move(message_ids=message_ids, destination_folder=destination_folder, folder=folder)

    @mcp.tool()
    def bulk_copy(message_ids: list[str], destination_folder: str, folder: str = "INBOX") -> dict:
        """Copy explicit message UIDs, capped by PROTON_MCP_BULK_LIMIT."""
        return mail.bulk_copy(message_ids=message_ids, destination_folder=destination_folder, folder=folder)

    @mcp.tool()
    def bulk_archive(message_ids: list[str], folder: str = "INBOX") -> dict:
        """Archive explicit message UIDs, capped by PROTON_MCP_BULK_LIMIT."""
        return mail.bulk_archive(message_ids=message_ids, folder=folder)

    @mcp.tool()
    def bulk_trash(message_ids: list[str], folder: str = "INBOX") -> dict:
        """Trash explicit message UIDs, capped by PROTON_MCP_BULK_LIMIT."""
        return mail.bulk_trash(message_ids=message_ids, folder=folder)

    @mcp.tool()
    def bulk_restore(message_ids: list[str], folder: str | None = None, destination_folder: str = "INBOX") -> dict:
        """Restore explicit message UIDs, capped by PROTON_MCP_BULK_LIMIT."""
        return mail.bulk_restore(
            message_ids=message_ids,
            folder=folder,
            destination_folder=destination_folder,
        )

    @mcp.tool()
    def bulk_permanently_delete(message_ids: list[str], folder: str = "INBOX", confirm: bool = False) -> dict:
        """Move explicit UIDs through Trash and expunge them after confirmation."""
        _require_confirmation(confirm, "permanently delete these messages")
        return mail.bulk_permanently_delete(message_ids=message_ids, folder=folder)

    @mcp.tool()
    def empty_trash(confirm: bool = False) -> dict:
        """Permanently empty the configured Trash folder after confirmation."""
        _require_confirmation(confirm, "permanently empty Trash")
        return mail.empty_folder(folder=settings.trash_folder)

    @mcp.tool()
    def empty_spam(confirm: bool = False) -> dict:
        """Move Spam messages through Trash and expunge them after confirmation."""
        _require_confirmation(confirm, "permanently empty Spam")
        return mail.empty_folder(folder=settings.spam_folder)

    @mcp.tool()
    def simplelogin_user_info() -> dict:
        """Get SimpleLogin account information."""
        return simplelogin.user_info()

    @mcp.tool()
    def simplelogin_stats() -> dict:
        """Get SimpleLogin alias and mail activity stats."""
        return simplelogin.stats()

    @mcp.tool()
    def simplelogin_list_aliases(
        page_id: int = 0,
        pinned: bool | None = None,
        disabled: bool | None = None,
        enabled: bool | None = None,
        query: str | None = None,
    ) -> dict:
        """List SimpleLogin aliases."""
        return simplelogin.list_aliases(page_id=page_id, pinned=pinned, disabled=disabled, enabled=enabled, query=query)

    @mcp.tool()
    def simplelogin_get_alias(alias_id: int) -> dict:
        """Get one SimpleLogin alias."""
        return simplelogin.get_alias(alias_id)

    @mcp.tool()
    def simplelogin_create_random_alias(
        hostname: str | None = None, mode: str | None = None, note: str | None = None
    ) -> dict:
        """Create a random SimpleLogin alias."""
        return simplelogin.create_random_alias(hostname=hostname, mode=mode, note=note)

    @mcp.tool()
    def simplelogin_create_custom_alias(
        alias_prefix: str,
        signed_suffix: str,
        mailbox_ids: list[int],
        hostname: str | None = None,
        note: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Create a custom SimpleLogin alias."""
        return simplelogin.create_custom_alias(
            alias_prefix=alias_prefix,
            signed_suffix=signed_suffix,
            mailbox_ids=mailbox_ids,
            hostname=hostname,
            note=note,
            name=name,
        )

    @mcp.tool()
    def simplelogin_update_alias(
        alias_id: int,
        note: str | None = None,
        mailbox_id: int | None = None,
        mailbox_ids: list[int] | None = None,
        name: str | None = None,
        disable_pgp: bool | None = None,
        pinned: bool | None = None,
    ) -> dict:
        """Update SimpleLogin alias metadata."""
        return simplelogin.update_alias(
            alias_id,
            note=note,
            mailbox_id=mailbox_id,
            mailbox_ids=mailbox_ids,
            name=name,
            disable_pgp=disable_pgp,
            pinned=pinned,
        )

    @mcp.tool()
    def simplelogin_toggle_alias(alias_id: int) -> dict:
        """Toggle a SimpleLogin alias enabled or disabled."""
        return simplelogin.toggle_alias(alias_id)

    @mcp.tool()
    def simplelogin_delete_alias(alias_id: int, confirm: bool = False) -> dict:
        """Delete a SimpleLogin alias."""
        _require_confirmation(confirm, "delete this SimpleLogin alias")
        return simplelogin.delete_alias(alias_id)

    @mcp.tool()
    def simplelogin_list_alias_contacts(alias_id: int, page_id: int = 0) -> dict:
        """List contacts for a SimpleLogin alias."""
        return simplelogin.list_alias_contacts(alias_id, page_id=page_id)

    @mcp.tool()
    def simplelogin_create_alias_contact(alias_id: int, contact: str) -> dict:
        """Create a contact for a SimpleLogin alias."""
        return simplelogin.create_alias_contact(alias_id, contact=contact)

    @mcp.tool()
    def simplelogin_list_mailboxes() -> dict:
        """List SimpleLogin mailboxes."""
        return simplelogin.list_mailboxes()

    @mcp.tool()
    def server_status() -> dict:
        """Check configured service availability without returning credentials."""
        bridge = mail.status()
        simplelogin_status: dict[str, object] = {"configured": bool(settings.simplelogin_api_key)}
        if settings.simplelogin_api_key:
            try:
                simplelogin.user_info()
                simplelogin_status["reachable"] = True
            except Exception as exc:
                simplelogin_status["reachable"] = False
                simplelogin_status["error"] = type(exc).__name__
        return {
            "version": __version__,
            "bridge": bridge,
            "simplelogin": simplelogin_status,
            "oauth_configured": bool(settings.oauth_issuer_url),
        }

    return mcp


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Proton Workflow Connector.")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="Host for streamable-http transport.")
    parser.add_argument("--port", type=int, default=8765, help="Port for streamable-http transport.")
    parser.add_argument("--env-file", help="Optional .env file to load before reading settings.")
    args = parser.parse_args(argv)

    if args.env_file:
        try:
            from dotenv import load_dotenv
        except ImportError as exc:
            raise RuntimeError("python-dotenv is required for --env-file support.") from exc
        load_dotenv(args.env_file)

    settings = load_settings()
    enable_http_auth = _validate_http_security(args.transport, args.host, settings)
    server = build_server(
        settings=settings,
        host=args.host,
        port=args.port,
        enable_http_auth=enable_http_auth,
    )
    try:
        server.run(transport=args.transport)
    except KeyboardInterrupt:
        pass


def _new_fastmcp(
    FastMCP,
    name: str,
    *,
    settings: Settings,
    host: str | None,
    port: int | None,
    enable_http_auth: bool,
):
    kwargs = {"instructions": SERVER_INSTRUCTIONS}
    if host is not None:
        kwargs["host"] = host
    if port is not None:
        kwargs["port"] = port
    if settings.http_allowed_hosts:
        from mcp.server.transport_security import TransportSecuritySettings

        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.http_allowed_hosts),
            allowed_origins=list(settings.http_allowed_origins),
        )
    if enable_http_auth:
        from mcp.server.auth.settings import AuthSettings
        from pydantic import AnyHttpUrl

        kwargs["token_verifier"] = OIDCTokenVerifier(
            issuer_url=settings.oauth_issuer_url,
            audience=settings.oauth_audience,
            jwks_url=settings.oauth_jwks_url,
        )
        kwargs["auth"] = AuthSettings(
            issuer_url=AnyHttpUrl(settings.oauth_issuer_url),
            resource_server_url=AnyHttpUrl(settings.oauth_resource_server_url),
            required_scopes=[settings.oauth_base_scope],
        )
    return FastMCP(name, **kwargs)


def _validate_http_security(transport: str, host: str, settings: Settings) -> bool:
    if transport != "streamable-http":
        return False
    oauth_values = [settings.oauth_issuer_url, settings.oauth_audience, settings.oauth_resource_server_url]
    if any(oauth_values) and not all(oauth_values):
        raise RuntimeError(
            "OAuth requires PROTON_MCP_OAUTH_ISSUER_URL, PROTON_MCP_OAUTH_AUDIENCE, "
            "and PROTON_MCP_OAUTH_RESOURCE_SERVER_URL"
        )
    oauth_enabled = all(oauth_values)
    if oauth_enabled:
        _require_https_or_loopback("PROTON_MCP_OAUTH_ISSUER_URL", settings.oauth_issuer_url)
        _require_https_or_loopback(
            "PROTON_MCP_OAUTH_RESOURCE_SERVER_URL",
            settings.oauth_resource_server_url,
        )
        if settings.oauth_jwks_url:
            _require_https_or_loopback("PROTON_MCP_OAUTH_JWKS_URL", settings.oauth_jwks_url)
    non_loopback = host not in {"127.0.0.1", "localhost", "::1"}
    if non_loopback and not settings.http_allowed_hosts:
        raise RuntimeError("Non-local HTTP requires PROTON_MCP_HTTP_ALLOWED_HOSTS")
    if non_loopback and not oauth_enabled and not settings.allow_unauthenticated_http:
        raise RuntimeError("Non-local HTTP requires OAuth or explicit PROTON_MCP_ALLOW_UNAUTHENTICATED_HTTP=true")
    return oauth_enabled


def _require_https_or_loopback(name: str, value: str) -> None:
    parsed = urlparse(value)
    if not parsed.hostname or parsed.scheme not in {"http", "https"}:
        raise RuntimeError(f"{name} must be an absolute HTTP or HTTPS URL")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} and parsed.scheme != "https":
        raise RuntimeError(f"Hosted OAuth requires an HTTPS {name}")


def _require_confirmation(confirm: bool, action: str) -> None:
    if confirm is not True:
        raise ValueError(f"Set confirm=true to {action}")

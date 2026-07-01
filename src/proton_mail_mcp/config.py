from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _env_list(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    value = env.get(name, "")
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    imap_host: str = "127.0.0.1"
    imap_port: int = 1143
    imap_tls: str = "starttls"
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_tls: str = "starttls"
    allow_insecure_tls: bool = True
    bridge_username: str = ""
    bridge_password: str = ""
    bridge_email: str = ""
    bridge_sender_addresses: tuple[str, ...] = ()
    simplelogin_api_key: str = ""
    simplelogin_base_url: str = "https://app.simplelogin.io"
    archive_folder: str = "Archive"
    labels_folder: str = "Labels"
    trash_folder: str = "Trash"
    drafts_folder: str = "Drafts"
    sent_folder: str = "Sent"
    spam_folder: str = "Spam"
    read_only: bool = False
    allow_send: bool = True
    allowed_actions: tuple[str, ...] = ()
    bulk_limit: int = 50
    body_preview_chars: int = 500
    max_body_chars: int = 20_000
    max_attachment_download_bytes: int = 50_000_000
    max_outgoing_attachment_bytes: int = 25_000_000
    max_attachments: int = 100
    search_all_limit: int = 100
    request_timeout: float = 30.0
    oauth_issuer_url: str = ""
    oauth_audience: str = ""
    oauth_resource_server_url: str = ""
    oauth_jwks_url: str = ""
    oauth_base_scope: str = "proton-workflow-connector"
    http_allowed_hosts: tuple[str, ...] = ()
    http_allowed_origins: tuple[str, ...] = ()
    allow_unauthenticated_http: bool = False
    audit_log: str = ""
    rate_limit_read: int = 120
    rate_limit_write: int = 30
    rate_limit_destructive: int = 5
    watch_state_path: str = ""
    watch_poll_interval: float = 60.0
    watch_folders: tuple[str, ...] = ("INBOX",)
    watch_webhook_url: str = ""
    watch_webhook_secret: str = ""
    watch_limit: int = 50
    watch_unread_only: bool = False
    watch_max_retries: int = 3
    watch_retry_backoff: float = 2.0
    watch_rules_path: str = ""
    watch_sink: str = "webhook"
    watch_file_path: str = ""
    watch_command: str = ""
    watch_dead_letter_path: str = ""
    watch_dead_letter_max_attempts: int = 5
    watch_idle: bool = False

    def require_bridge(self) -> None:
        missing = [
            name
            for name, value in {
                "PROTON_BRIDGE_USERNAME": self.bridge_username,
                "PROTON_BRIDGE_PASSWORD": self.bridge_password,
                "PROTON_BRIDGE_EMAIL": self.bridge_email,
            }.items()
            if not value
        ]
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Missing required Proton Bridge settings: {joined}")

    def require_simplelogin(self) -> None:
        if not self.simplelogin_api_key:
            raise RuntimeError("Missing required SimpleLogin setting: SIMPLELOGIN_API_KEY")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    return Settings(
        imap_host=source.get("PROTON_BRIDGE_IMAP_HOST", "127.0.0.1"),
        imap_port=_env_int(source, "PROTON_BRIDGE_IMAP_PORT", 1143),
        imap_tls=source.get("PROTON_BRIDGE_IMAP_TLS", "starttls").lower(),
        smtp_host=source.get("PROTON_BRIDGE_SMTP_HOST", "127.0.0.1"),
        smtp_port=_env_int(source, "PROTON_BRIDGE_SMTP_PORT", 1025),
        smtp_tls=source.get("PROTON_BRIDGE_SMTP_TLS", "starttls").lower(),
        allow_insecure_tls=_env_bool(source, "PROTON_BRIDGE_ALLOW_INSECURE_TLS", True),
        bridge_username=source.get("PROTON_BRIDGE_USERNAME", ""),
        bridge_password=source.get("PROTON_BRIDGE_PASSWORD", ""),
        bridge_email=source.get("PROTON_BRIDGE_EMAIL", source.get("PROTON_BRIDGE_USERNAME", "")),
        bridge_sender_addresses=_env_list(source, "PROTON_BRIDGE_SENDER_ADDRESSES"),
        simplelogin_api_key=source.get("SIMPLELOGIN_API_KEY", ""),
        simplelogin_base_url=source.get("SIMPLELOGIN_BASE_URL", "https://app.simplelogin.io").rstrip("/"),
        archive_folder=source.get("PROTON_ARCHIVE_FOLDER", "Archive"),
        labels_folder=source.get("PROTON_LABELS_FOLDER", "Labels"),
        trash_folder=source.get("PROTON_TRASH_FOLDER", "Trash"),
        drafts_folder=source.get("PROTON_DRAFTS_FOLDER", "Drafts"),
        sent_folder=source.get("PROTON_SENT_FOLDER", "Sent"),
        spam_folder=source.get("PROTON_SPAM_FOLDER", "Spam"),
        read_only=_env_bool(source, "PROTON_MCP_READ_ONLY", False),
        allow_send=_env_bool(source, "PROTON_MCP_ALLOW_SEND", True),
        allowed_actions=_env_list(source, "PROTON_MCP_ALLOWED_ACTIONS"),
        bulk_limit=_env_int(source, "PROTON_MCP_BULK_LIMIT", 50),
        body_preview_chars=_env_int(source, "PROTON_MCP_BODY_PREVIEW_CHARS", 500),
        max_body_chars=_env_int(source, "PROTON_MCP_MAX_BODY_CHARS", 20_000),
        max_attachment_download_bytes=_env_int(source, "PROTON_MCP_MAX_ATTACHMENT_DOWNLOAD_BYTES", 50_000_000),
        max_outgoing_attachment_bytes=_env_int(source, "PROTON_MCP_MAX_OUTGOING_ATTACHMENT_BYTES", 25_000_000),
        max_attachments=_env_int(source, "PROTON_MCP_MAX_ATTACHMENTS", 100),
        search_all_limit=_env_int(source, "PROTON_MCP_SEARCH_ALL_LIMIT", 100),
        request_timeout=float(source.get("PROTON_MCP_REQUEST_TIMEOUT", "30")),
        oauth_issuer_url=source.get("PROTON_MCP_OAUTH_ISSUER_URL", "").rstrip("/"),
        oauth_audience=source.get("PROTON_MCP_OAUTH_AUDIENCE", ""),
        oauth_resource_server_url=source.get("PROTON_MCP_OAUTH_RESOURCE_SERVER_URL", "").rstrip("/"),
        oauth_jwks_url=source.get("PROTON_MCP_OAUTH_JWKS_URL", ""),
        oauth_base_scope=source.get("PROTON_MCP_OAUTH_BASE_SCOPE", "proton-workflow-connector"),
        http_allowed_hosts=_env_list(source, "PROTON_MCP_HTTP_ALLOWED_HOSTS"),
        http_allowed_origins=_env_list(source, "PROTON_MCP_HTTP_ALLOWED_ORIGINS"),
        allow_unauthenticated_http=_env_bool(source, "PROTON_MCP_ALLOW_UNAUTHENTICATED_HTTP", False),
        audit_log=source.get("PROTON_MCP_AUDIT_LOG", ""),
        rate_limit_read=_env_int(source, "PROTON_MCP_RATE_LIMIT_READ", 120),
        rate_limit_write=_env_int(source, "PROTON_MCP_RATE_LIMIT_WRITE", 30),
        rate_limit_destructive=_env_int(source, "PROTON_MCP_RATE_LIMIT_DESTRUCTIVE", 5),
        watch_state_path=source.get("PROTON_MCP_WATCH_STATE", ""),
        watch_poll_interval=float(source.get("PROTON_MCP_WATCH_INTERVAL", "60")),
        watch_folders=_env_list(source, "PROTON_MCP_WATCH_FOLDERS") or ("INBOX",),
        watch_webhook_url=source.get("PROTON_MCP_WATCH_WEBHOOK_URL", ""),
        watch_webhook_secret=source.get("PROTON_MCP_WATCH_WEBHOOK_SECRET", ""),
        watch_limit=_env_int(source, "PROTON_MCP_WATCH_LIMIT", 50),
        watch_unread_only=_env_bool(source, "PROTON_MCP_WATCH_UNREAD_ONLY", False),
        watch_max_retries=_env_int(source, "PROTON_MCP_WATCH_MAX_RETRIES", 3),
        watch_retry_backoff=float(source.get("PROTON_MCP_WATCH_RETRY_BACKOFF", "2")),
        watch_rules_path=source.get("PROTON_MCP_WATCH_RULES", ""),
        watch_sink=source.get("PROTON_MCP_WATCH_SINK", "webhook").strip().lower() or "webhook",
        watch_file_path=source.get("PROTON_MCP_WATCH_FILE", ""),
        watch_command=source.get("PROTON_MCP_WATCH_COMMAND", ""),
        watch_dead_letter_path=source.get("PROTON_MCP_WATCH_DEAD_LETTER", ""),
        watch_dead_letter_max_attempts=_env_int(source, "PROTON_MCP_WATCH_DEAD_LETTER_MAX_ATTEMPTS", 5),
        watch_idle=_env_bool(source, "PROTON_MCP_WATCH_IDLE", False),
    )

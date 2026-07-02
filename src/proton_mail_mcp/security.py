from __future__ import annotations

import functools
import json
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token

from .config import Settings


@dataclass(frozen=True)
class OperationPolicy:
    scope: str
    category: str


MAIL_POLICIES = {
    "list_folders": OperationPolicy("mail.read", "read"),
    "folder_status": OperationPolicy("mail.read", "read"),
    "search_mail": OperationPolicy("mail.read", "read"),
    "search_all_mail": OperationPolicy("mail.read", "read"),
    "poll_folder": OperationPolicy("mail.read", "read"),
    "read_mail": OperationPolicy("mail.read", "read"),
    "read_thread": OperationPolicy("mail.read", "read"),
    "inspect_attachments": OperationPolicy("mail.read", "read"),
    "download_attachment": OperationPolicy("mail.read", "read"),
    "get_headers": OperationPolicy("mail.read", "read"),
    "list_labels": OperationPolicy("mail.read", "read"),
    "status": OperationPolicy("mail.read", "read"),
    "send_mail": OperationPolicy("mail.write", "write"),
    "reply_mail": OperationPolicy("mail.write", "write"),
    "draft_reply": OperationPolicy("mail.write", "write"),
    "forward_mail": OperationPolicy("mail.write", "write"),
    "draft_forward": OperationPolicy("mail.write", "write"),
    "apply_label": OperationPolicy("mail.write", "write"),
    "remove_label": OperationPolicy("mail.write", "write"),
    "unsubscribe": OperationPolicy("mail.write", "write"),
    "create_draft": OperationPolicy("mail.write", "write"),
    "update_draft": OperationPolicy("mail.write", "write"),
    "send_draft": OperationPolicy("mail.write", "write"),
    "delete_draft": OperationPolicy("mail.delete", "destructive"),
    "mark_read": OperationPolicy("mail.write", "write"),
    "mark_unread": OperationPolicy("mail.write", "write"),
    "star_message": OperationPolicy("mail.write", "write"),
    "unstar_message": OperationPolicy("mail.write", "write"),
    "move_message": OperationPolicy("mail.write", "write"),
    "copy_message": OperationPolicy("mail.write", "write"),
    "archive_message": OperationPolicy("mail.write", "write"),
    "trash_message": OperationPolicy("mail.write", "write"),
    "restore_message": OperationPolicy("mail.write", "write"),
    "mark_spam": OperationPolicy("mail.write", "write"),
    "mark_not_spam": OperationPolicy("mail.write", "write"),
    "create_folder": OperationPolicy("mail.write", "write"),
    "rename_folder": OperationPolicy("mail.write", "write"),
    "delete_folder": OperationPolicy("mail.delete", "destructive"),
    "subscribe_folder": OperationPolicy("mail.write", "write"),
    "unsubscribe_folder": OperationPolicy("mail.write", "write"),
    "permanently_delete_message": OperationPolicy("mail.delete", "destructive"),
    "bulk_mark_read": OperationPolicy("mail.write", "write"),
    "bulk_mark_unread": OperationPolicy("mail.write", "write"),
    "bulk_star": OperationPolicy("mail.write", "write"),
    "bulk_unstar": OperationPolicy("mail.write", "write"),
    "bulk_move": OperationPolicy("mail.write", "write"),
    "bulk_copy": OperationPolicy("mail.write", "write"),
    "bulk_archive": OperationPolicy("mail.write", "write"),
    "bulk_trash": OperationPolicy("mail.write", "write"),
    "bulk_restore": OperationPolicy("mail.write", "write"),
    "bulk_permanently_delete": OperationPolicy("mail.delete", "destructive"),
    "empty_folder": OperationPolicy("mail.delete", "destructive"),
}

SIMPLELOGIN_POLICIES = {
    "user_info": OperationPolicy("simplelogin.read", "read"),
    "stats": OperationPolicy("simplelogin.read", "read"),
    "list_aliases": OperationPolicy("simplelogin.read", "read"),
    "poll_aliases": OperationPolicy("simplelogin.read", "read"),
    "get_alias": OperationPolicy("simplelogin.read", "read"),
    "get_alias_options": OperationPolicy("simplelogin.read", "read"),
    "list_alias_contacts": OperationPolicy("simplelogin.read", "read"),
    "list_mailboxes": OperationPolicy("simplelogin.read", "read"),
    "create_random_alias": OperationPolicy("simplelogin.write", "write"),
    "create_custom_alias": OperationPolicy("simplelogin.write", "write"),
    "update_alias": OperationPolicy("simplelogin.write", "write"),
    "toggle_alias": OperationPolicy("simplelogin.write", "write"),
    "create_alias_contact": OperationPolicy("simplelogin.write", "write"),
    "delete_alias": OperationPolicy("simplelogin.delete", "destructive"),
}


SEND_METHODS = frozenset({"send_mail", "reply_mail", "forward_mail", "send_draft"})


class OperationGuard:
    def __init__(self, settings: Settings, *, enforce_auth: bool) -> None:
        self.settings = settings
        self.enforce_auth = enforce_auth
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _enforce_mode(self, policy: OperationPolicy, name: str, kwargs: dict[str, Any]) -> None:
        """Block writes/sends up front based on read-only, allow-send, and allowed-actions settings."""
        if kwargs.get("dry_run"):
            return  # a dry-run mutates nothing, so previews are allowed even in read-only mode
        if self.settings.read_only and policy.category in ("write", "destructive"):
            raise PermissionError(f"{name!r} is blocked: server is read-only (PROTON_MCP_READ_ONLY)")
        if not self.settings.allow_send and name in SEND_METHODS:
            raise PermissionError(f"{name!r} is blocked: sending is disabled (PROTON_MCP_ALLOW_SEND=false)")
        if self.settings.allowed_actions and policy.category not in self.settings.allowed_actions:
            allowed = ", ".join(self.settings.allowed_actions)
            raise PermissionError(f"{name!r} ({policy.category}) is not in PROTON_MCP_ALLOWED_ACTIONS ({allowed})")

    def invoke(self, policy: OperationPolicy, name: str, function, args: tuple[Any, ...], kwargs: dict[str, Any]):
        try:
            self._enforce_mode(policy, name, kwargs)
        except PermissionError as exc:
            # Blocked operations are audit-worthy events: record what was refused and why.
            self._audit("local", policy, name, kwargs, outcome="blocked", error=str(exc))
            raise
        actor = self._authorize_and_limit(policy)
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            self._audit(actor, policy, name, kwargs, outcome="error", error=type(exc).__name__)
            raise
        self._audit(actor, policy, name, kwargs, outcome="success")
        return result

    def _authorize_and_limit(self, policy: OperationPolicy) -> str:
        token = get_access_token()
        if self.enforce_auth:
            if token is None:
                raise PermissionError("OAuth access token is required")
            if policy.scope not in token.scopes and "proton-workflow-connector.admin" not in token.scopes:
                raise PermissionError(f"OAuth token is missing required scope: {policy.scope}")
        actor = "local"
        if token is not None:
            actor = token.subject or token.client_id
            self._rate_limit(actor, policy.category)
        return actor

    def _rate_limit(self, actor: str, category: str) -> None:
        limit = {
            "read": self.settings.rate_limit_read,
            "write": self.settings.rate_limit_write,
            "destructive": self.settings.rate_limit_destructive,
        }[category]
        if limit <= 0:
            return
        now = time.monotonic()
        key = (actor, category)
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - 60:
                events.popleft()
            if len(events) >= limit:
                raise RuntimeError(f"Rate limit exceeded for {category} operations")
            events.append(now)

    def _audit(
        self,
        actor: str,
        policy: OperationPolicy,
        name: str,
        kwargs: dict[str, Any],
        *,
        outcome: str,
        error: str | None = None,
    ) -> None:
        if not self.settings.audit_log or policy.category == "read":
            return
        safe_keys = {
            "folder",
            "destination_folder",
            "message_id",
            "message_ids",
            "alias_id",
            "name",
            "new_name",
        }
        record = {
            "timestamp": int(time.time()),
            "actor": actor,
            "operation": name,
            "category": policy.category,
            "outcome": outcome,
            "target": {key: kwargs[key] for key in safe_keys if key in kwargs},
        }
        if error:
            record["error"] = error
        path = Path(self.settings.audit_log).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, (json.dumps(record, sort_keys=True) + "\n").encode())
        finally:
            os.close(descriptor)


class GuardedClient:
    def __init__(self, client: Any, guard: OperationGuard, policies: dict[str, OperationPolicy]) -> None:
        self._client = client
        self._guard = guard
        self._policies = policies

    def __getattr__(self, name: str):
        value = getattr(self._client, name)
        policy = self._policies.get(name)
        if policy is None or not callable(value):
            return value

        @functools.wraps(value)
        def guarded(*args, **kwargs):
            return self._guard.invoke(policy, name, value, args, kwargs)

        return guarded

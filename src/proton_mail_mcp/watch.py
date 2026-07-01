"""Mailbox trigger layer: detect new Proton Mail through Bridge and deliver it to the rest of your stack.

This turns the connector from a request/response tool server into a workflow connector. A watcher
polls one or more IMAP folders with UIDVALIDITY-aware cursors and emits an event per new message.
Events can be pulled on demand (via the ``poll_mailbox`` MCP tool) or pushed to any HTTP endpoint
(n8n, Zapier, Make, a serverless function, your own service) with an optional HMAC signature so the
receiver can verify the payload really came from your connector.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .imap_client import BridgeMailClient
from .redaction import redact_text

logger = logging.getLogger("proton_workflow_connector.watch")

EVENT_TYPE = "mail.received"
SIGNATURE_HEADER = "X-Proton-Signature"
EVENT_HEADER = "X-Proton-Event"


@dataclass(frozen=True)
class WatchRule:
    """One folder to watch and the filter that decides which new messages become events."""

    name: str
    folder: str = "INBOX"
    query: str | None = None
    from_: str | None = None
    to: str | None = None
    subject: str | None = None
    unread: bool | None = None
    starred: bool | None = None
    limit: int = 50

    def criteria(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "from_": self.from_,
            "to": self.to,
            "subject": self.subject,
            "unread": self.unread,
            "starred": self.starred,
            "limit": self.limit,
        }


@dataclass
class CursorStore:
    """Small JSON file mapping a cursor name to its last-seen IMAP UID and UIDVALIDITY.

    Persisting the cursor is what makes triggers reliable across restarts: a new run resumes from
    the last delivered message instead of replaying history or missing mail that arrived while down.
    """

    path: Path
    _data: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> CursorStore:
        resolved = Path(path).expanduser()
        data: dict[str, dict[str, Any]] = {}
        if resolved.exists():
            try:
                loaded = json.loads(resolved.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = {str(key): dict(value) for key, value in loaded.items() if isinstance(value, dict)}
            except (json.JSONDecodeError, OSError):
                logger.warning("Could not read cursor store at %s; starting fresh", resolved)
        return cls(path=resolved, _data=data)

    def get(self, name: str) -> tuple[int, int | None]:
        entry = self._data.get(name, {})
        last_uid = int(entry.get("cursor_uid", 0) or 0)
        uid_validity = entry.get("uid_validity")
        return last_uid, int(uid_validity) if uid_validity is not None else None

    def set(self, name: str, *, cursor_uid: int, uid_validity: int | None) -> None:
        self._data[name] = {"cursor_uid": int(cursor_uid), "uid_validity": uid_validity}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, (json.dumps(self._data, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)


def default_state_path(settings: Settings) -> Path:
    """Resolve where cursors live, honouring the configured path then XDG_STATE_HOME."""
    if settings.watch_state_path:
        return Path(settings.watch_state_path).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "proton-workflow-connector" / "watch-state.json"


def sign_payload(secret: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` HMAC a webhook receiver can compare against ``X-Proton-Signature``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_event(rule_name: str, folder: str, message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": EVENT_TYPE,
        "rule": rule_name,
        "folder": folder,
        "message": dict(message),
        "timestamp": int(time.time()),
    }


def deliver_webhook(
    url: str,
    event: Mapping[str, Any],
    *,
    secret: str = "",
    timeout: float = 30.0,
    transport: Any | None = None,
) -> int:
    """POST one event as JSON. Returns the HTTP status code. ``transport`` is injectable for tests."""
    import httpx

    body = json.dumps(event, sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json", EVENT_HEADER: str(event.get("type", EVENT_TYPE))}
    if secret:
        headers[SIGNATURE_HEADER] = sign_payload(secret, body)
    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.post(url, content=body, headers=headers)
    return response.status_code


def poll_rule(
    client: BridgeMailClient,
    rule: WatchRule,
    store: CursorStore,
) -> list[dict[str, Any]]:
    """Poll one rule, advance its cursor, and return the events for any new messages found."""
    last_uid, uid_validity = store.get(rule.name)
    result = client.poll_folder(folder=rule.folder, last_uid=last_uid, uid_validity=uid_validity, **rule.criteria())
    store.set(rule.name, cursor_uid=result["cursor_uid"], uid_validity=result.get("uid_validity"))
    if result.get("baseline"):
        logger.info("Baselined rule %r at UID %s (no backlog delivered)", rule.name, result["cursor_uid"])
    if result.get("reset"):
        logger.warning("UIDVALIDITY changed for rule %r; re-baselined at UID %s", rule.name, result["cursor_uid"])
    return [build_event(rule.name, rule.folder, message) for message in result["messages"]]


def rules_from_settings(settings: Settings) -> list[WatchRule]:
    unread = True if settings.watch_unread_only else None
    return [
        WatchRule(name=folder, folder=folder, unread=unread, limit=settings.watch_limit)
        for folder in settings.watch_folders
    ]


def run_watch(
    settings: Settings,
    *,
    rules: Sequence[WatchRule] | None = None,
    client: BridgeMailClient | None = None,
    store: CursorStore | None = None,
    once: bool = False,
    sink: Any | None = None,
) -> int:
    """Run the polling loop. ``sink`` receives each event (defaults to webhook delivery). Returns events seen."""
    settings.require_bridge()
    if not settings.watch_webhook_url and sink is None:
        raise RuntimeError("Set PROTON_MCP_WATCH_WEBHOOK_URL or pass a sink to run the watcher")
    client = client or BridgeMailClient(settings)
    store = store or CursorStore.load(default_state_path(settings))
    rules = list(rules) if rules is not None else rules_from_settings(settings)

    def emit(event: dict[str, Any]) -> None:
        if sink is not None:
            sink(event)
            return
        status = deliver_webhook(
            settings.watch_webhook_url,
            event,
            secret=settings.watch_webhook_secret,
            timeout=settings.request_timeout,
        )
        if status >= 400:
            logger.error("Webhook rejected event for rule %r with HTTP %s", event.get("rule"), status)
        else:
            logger.info("Delivered %s event for rule %r (HTTP %s)", event.get("type"), event.get("rule"), status)

    total = 0
    interval = max(settings.watch_poll_interval, 1.0)
    logger.info("Watching %d folder(s) every %.0fs", len(rules), interval)
    while True:
        for rule in rules:
            try:
                events = poll_rule(client, rule, store)
            except Exception as exc:  # keep the loop alive; one bad poll should not stop the watcher
                logger.error("Poll failed for rule %r: %s", rule.name, redact_text(str(exc)))
                continue
            for event in events:
                total += 1
                try:
                    emit(event)
                except Exception as exc:
                    logger.error("Delivery failed for rule %r: %s", rule.name, redact_text(str(exc)))
        store.save()
        if once:
            return total
        time.sleep(interval)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="proton-workflow-watch",
        description="Watch Proton Mail through Bridge and push new-message events to a webhook.",
    )
    parser.add_argument("--env-file", help="Optional .env file to load before reading settings.")
    parser.add_argument("--folder", action="append", dest="folders", help="Folder to watch (repeatable).")
    parser.add_argument("--webhook-url", help="Override PROTON_MCP_WATCH_WEBHOOK_URL.")
    parser.add_argument("--interval", type=float, help="Seconds between polls.")
    parser.add_argument("--unread-only", action="store_true", help="Only emit events for unread messages.")
    parser.add_argument("--once", action="store_true", help="Poll a single time and exit (useful for cron).")
    parser.add_argument("--state-path", help="Override the cursor state file location.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO), format="%(message)s")

    if args.env_file:
        try:
            from dotenv import load_dotenv
        except ImportError as exc:
            raise RuntimeError("python-dotenv is required for --env-file support.") from exc
        load_dotenv(args.env_file)

    settings = load_settings()
    overrides: dict[str, Any] = {}
    if args.folders:
        overrides["watch_folders"] = tuple(args.folders)
    if args.webhook_url:
        overrides["watch_webhook_url"] = args.webhook_url
    if args.interval is not None:
        overrides["watch_poll_interval"] = args.interval
    if args.unread_only:
        overrides["watch_unread_only"] = True
    if args.state_path:
        overrides["watch_state_path"] = args.state_path
    if overrides:
        settings = replace_settings(settings, **overrides)

    try:
        count = run_watch(settings, once=args.once)
        if args.once:
            logger.info("Delivered %d event(s)", count)
    except KeyboardInterrupt:
        pass


def replace_settings(settings: Settings, **overrides: Any) -> Settings:
    from dataclasses import replace

    return replace(settings, **overrides)


if __name__ == "__main__":  # pragma: no cover
    main()

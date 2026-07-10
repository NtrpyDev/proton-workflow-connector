"""Trigger layer: detect Proton Mail and SimpleLogin activity and deliver it to the rest of your stack.

This turns the connector from a request/response tool server into a workflow connector. A watcher
polls one or more event sources with persisted cursors and emits an event per new item:

- ``mail.received`` — new messages in an IMAP folder (UIDVALIDITY-aware UID cursor).
- ``alias.created`` — new SimpleLogin aliases (cursor by maximum alias id).

Events can be pulled on demand (via the ``poll_mailbox`` MCP tool) or pushed to a delivery target:
an HTTP webhook (default, optionally HMAC-signed), an appended JSONL file, or an external command.
Delivery is at-least-once; an event that keeps failing is written to a dead-letter file after a
configurable number of cycles so one bad event can never stall a source forever.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import hmac
import json
import logging
import os
import shlex
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .imap_client import BridgeMailClient
from .redaction import redact_text
from .security import MAIL_POLICIES, GuardedClient, OperationBlocked, OperationGuard
from .simplelogin_client import SimpleLoginClient

logger = logging.getLogger("proton_workflow_connector.watch")

EVENT_TYPE = "mail.received"
ALIAS_EVENT_TYPE = "alias.created"
SIGNATURE_HEADER = "X-Proton-Signature"
EVENT_HEADER = "X-Proton-Event"

SOURCE_MAIL = "mail"
SOURCE_SIMPLELOGIN_ALIAS = "simplelogin_alias"
KNOWN_SOURCES = {SOURCE_MAIL, SOURCE_SIMPLELOGIN_ALIAS}

Sink = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class WatchRule:
    """One event source to watch and the filter that decides which new items become events.

    ``source`` selects the event source (``mail`` or ``simplelogin_alias``). Mail rules use the
    IMAP filter fields; alias rules use ``query`` as an email substring match. ``webhook_url``
    overrides the global delivery target for just this rule (useful for fan-out to several stacks).
    """

    name: str
    source: str = SOURCE_MAIL
    folder: str = "INBOX"
    query: str | None = None
    from_: str | None = None
    to: str | None = None
    subject: str | None = None
    unread: bool | None = None
    starred: bool | None = None
    limit: int = 50
    webhook_url: str | None = None
    actions: tuple[dict[str, Any], ...] = field(default=(), compare=False)

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
    """Small JSON file mapping a cursor name to its last-seen position and delivery failure state.

    Persisting the cursor is what makes triggers reliable across restarts: a new run resumes from
    the last delivered item instead of replaying history or missing activity that happened while
    down. Each entry also records how many consecutive cycles the next item has failed to deliver,
    so the dead-letter guard survives restarts and ``--once`` cron invocations.
    """

    path: Path
    _data: dict[str, dict[str, Any]] = field(default_factory=dict)
    _base_data: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _dirty_cursors: set[str] = field(default_factory=set, repr=False)
    _dirty_failures: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> CursorStore:
        resolved = Path(path).expanduser()
        with _cursor_lock(resolved):
            data = _read_cursor_data(resolved)
        return cls(path=resolved, _data=copy.deepcopy(data), _base_data=copy.deepcopy(data))

    def get(self, name: str) -> tuple[int, int | None]:
        entry = self._data.get(name, {})
        last_uid = int(entry.get("cursor_uid", 0) or 0)
        uid_validity = entry.get("uid_validity")
        return last_uid, int(uid_validity) if uid_validity is not None else None

    def set(self, name: str, *, cursor_uid: int, uid_validity: int | None) -> None:
        entry = dict(self._data.get(name, {}))
        entry["cursor_uid"] = int(cursor_uid)
        entry["uid_validity"] = uid_validity
        self._data[name] = entry
        self._dirty_cursors.add(name)

    def get_failure(self, name: str) -> tuple[int | None, int]:
        """Return the cursor position of the currently-stuck item and how many cycles it has failed."""
        entry = self._data.get(name, {})
        fail_cursor = entry.get("fail_cursor")
        return (int(fail_cursor) if fail_cursor is not None else None), int(entry.get("fail_count", 0) or 0)

    def set_failure(self, name: str, *, fail_cursor: int, fail_count: int) -> None:
        entry = dict(self._data.get(name, {}))
        entry["fail_cursor"] = int(fail_cursor)
        entry["fail_count"] = int(fail_count)
        self._data[name] = entry
        self._dirty_failures.add(name)

    def clear_failure(self, name: str) -> None:
        entry = self._data.get(name)
        if entry is not None:
            entry.pop("fail_cursor", None)
            entry.pop("fail_count", None)
            self._dirty_failures.add(name)

    def save(self) -> None:
        with _cursor_lock(self.path, exclusive=True):
            merged = _read_cursor_data(self.path)
            for name in self._dirty_cursors | self._dirty_failures:
                merged[name] = self._merge_entry(name, merged.get(name, {}))
            _atomic_write_cursor_data(self.path, merged)
        self._data = copy.deepcopy(merged)
        self._base_data = copy.deepcopy(merged)
        self._dirty_cursors.clear()
        self._dirty_failures.clear()

    def _merge_entry(self, name: str, current: dict[str, Any]) -> dict[str, Any]:
        """Three-way merge this instance's changed fields into the latest on-disk entry."""
        merged = dict(current)
        local = self._data.get(name, {})
        base = self._base_data.get(name, {})
        if name in self._dirty_cursors:
            local_pair = (local.get("cursor_uid", 0), local.get("uid_validity"))
            base_pair = (base.get("cursor_uid", 0), base.get("uid_validity"))
            current_pair = (current.get("cursor_uid", 0), current.get("uid_validity"))
            if current_pair != base_pair and local_pair[1] == current_pair[1]:
                local_pair = (max(int(local_pair[0] or 0), int(current_pair[0] or 0)), local_pair[1])
            elif current_pair != base_pair and local_pair == base_pair:
                local_pair = current_pair
            merged["cursor_uid"], merged["uid_validity"] = local_pair
        if name in self._dirty_failures:
            local_pair = (local.get("fail_cursor"), local.get("fail_count", 0))
            base_pair = (base.get("fail_cursor"), base.get("fail_count", 0))
            current_pair = (current.get("fail_cursor"), current.get("fail_count", 0))
            if current_pair != base_pair and local_pair == base_pair:
                local_pair = current_pair
            elif current_pair != base_pair and local_pair[0] is not None and local_pair[0] == current_pair[0]:
                local_pair = (local_pair[0], max(int(local_pair[1] or 0), int(current_pair[1] or 0)))
            if local_pair[0] is None:
                merged.pop("fail_cursor", None)
                merged.pop("fail_count", None)
            else:
                merged["fail_cursor"], merged["fail_count"] = local_pair
        return merged


@contextmanager
def _cursor_lock(path: Path, *, exclusive: bool = False):
    """Coordinate cursor readers and writers through a stable sidecar lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_cursor_data(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Cursor store at {path} is corrupt or unreadable; refusing to baseline") from exc
    if not isinstance(loaded, dict) or any(not isinstance(value, dict) for value in loaded.values()):
        raise RuntimeError(f"Cursor store at {path} is corrupt; expected a JSON object of cursor entries")
    return {str(key): dict(value) for key, value in loaded.items()}


def _atomic_write_cursor_data(path: Path, data: Mapping[str, Any]) -> None:
    payload = (json.dumps(data, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary_path.unlink(missing_ok=True)


def default_state_path(settings: Settings) -> Path:
    """Resolve where cursors live, honouring the configured path then XDG_STATE_HOME."""
    if settings.watch_state_path:
        return Path(settings.watch_state_path).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "proton-workflow-connector" / "watch-state.json"


def default_dead_letter_path(settings: Settings) -> Path:
    """Resolve where undeliverable events are parked, defaulting next to the cursor state file."""
    if settings.watch_dead_letter_path:
        return Path(settings.watch_dead_letter_path).expanduser()
    return default_state_path(settings).parent / "dead-letter.jsonl"


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


def build_alias_event(rule_name: str, alias: Mapping[str, Any]) -> dict[str, Any]:
    fields = ("id", "email", "name", "note", "enabled", "creation_date", "creation_timestamp")
    payload = {key: alias[key] for key in fields if key in alias}
    return {
        "type": ALIAS_EVENT_TYPE,
        "rule": rule_name,
        "alias": payload,
        "timestamp": int(time.time()),
    }


class WebhookDeliveryError(RuntimeError):
    """Raised when an event could not be delivered to a webhook after exhausting retries."""


class CommandDeliveryError(RuntimeError):
    """Raised when the command sink exits non-zero for an event."""


def deliver_webhook(
    url: str,
    event: Mapping[str, Any],
    *,
    secret: str = "",
    timeout: float = 30.0,
    attempts: int = 1,
    backoff: float = 2.0,
    transport: Any | None = None,
    sleep: Any = time.sleep,
) -> int:
    """POST one event as JSON, retrying transient failures. Returns the HTTP status code on success.

    A 2xx/3xx response succeeds. Server errors (>=500) and 429 are retried with exponential backoff
    up to ``attempts`` times; a network error is treated the same way. Other 4xx responses are
    configuration problems, so they fail immediately. After the final attempt a
    :class:`WebhookDeliveryError` is raised so the caller can hold the cursor and retry next cycle.
    """
    import httpx

    body = json.dumps(event, sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json", EVENT_HEADER: str(event.get("type", EVENT_TYPE))}
    if secret:
        headers[SIGNATURE_HEADER] = sign_payload(secret, body)

    total = max(attempts, 1)
    last_detail = ""
    for attempt in range(total):
        try:
            with httpx.Client(timeout=timeout, transport=transport) as client:
                response = client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            last_detail = f"network error: {redact_text(str(exc))}"
        else:
            if response.status_code < 400:
                return response.status_code
            if response.status_code != 429 and response.status_code < 500:
                raise WebhookDeliveryError(f"Webhook rejected event with HTTP {response.status_code}")
            last_detail = f"HTTP {response.status_code}"
        if attempt < total - 1:
            sleep(backoff * (2**attempt))
    raise WebhookDeliveryError(f"Webhook delivery failed after {total} attempt(s): {last_detail}")


def _run_command(argv: Sequence[str], body: bytes, timeout: float) -> tuple[int, str]:
    import subprocess

    proc = subprocess.run(list(argv), input=body, capture_output=True, timeout=timeout)  # noqa: S603
    return proc.returncode, proc.stderr.decode("utf-8", errors="replace")


def make_webhook_sink(settings: Settings, url: str) -> Sink:
    def sink(event: Mapping[str, Any]) -> None:
        status = deliver_webhook(
            url,
            event,
            secret=settings.watch_webhook_secret,
            timeout=settings.request_timeout,
            attempts=settings.watch_max_retries,
            backoff=settings.watch_retry_backoff,
        )
        logger.info(
            "Delivered %s event for rule %r via webhook (HTTP %s)", event.get("type"), event.get("rule"), status
        )

    return sink


def make_file_sink(path: str | os.PathLike[str]) -> Sink:
    resolved = Path(path).expanduser()

    def sink(event: Mapping[str, Any]) -> None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(resolved, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)
        logger.info("Wrote %s event for rule %r to %s", event.get("type"), event.get("rule"), resolved)

    return sink


def make_command_sink(
    command: str,
    *,
    timeout: float = 30.0,
    runner: Callable[[Sequence[str], bytes, float], tuple[int, str]] | None = None,
) -> Sink:
    argv = shlex.split(command)
    if not argv:
        raise RuntimeError("Command sink requires a non-empty command (PROTON_MCP_WATCH_COMMAND)")
    run = runner or _run_command

    def sink(event: Mapping[str, Any]) -> None:
        body = json.dumps(event, sort_keys=True).encode("utf-8")
        code, detail = run(argv, body, timeout)
        if code != 0:
            raise CommandDeliveryError(f"Command {argv[0]!r} exited {code}: {redact_text(detail)}")
        logger.info("Piped %s event for rule %r to command %r", event.get("type"), event.get("rule"), argv[0])

    return sink


def resolve_sink(
    settings: Settings,
    rule: WatchRule,
    *,
    override: Sink | None = None,
    command_runner: Callable[[Sequence[str], bytes, float], tuple[int, str]] | None = None,
) -> Sink:
    """Pick the delivery target for a rule. A per-rule ``webhook_url`` always wins over the default."""
    if override is not None:
        return override
    if rule.webhook_url:
        return make_webhook_sink(settings, rule.webhook_url)
    sink_type = settings.watch_sink
    if sink_type == "webhook":
        if not settings.watch_webhook_url:
            raise RuntimeError(
                f"No webhook URL configured for rule {rule.name!r}; set PROTON_MCP_WATCH_WEBHOOK_URL, "
                "give the rule a webhook_url, or choose --sink file/command"
            )
        return make_webhook_sink(settings, settings.watch_webhook_url)
    if sink_type == "file":
        if not settings.watch_file_path:
            raise RuntimeError("The file sink requires PROTON_MCP_WATCH_FILE (or --file)")
        return make_file_sink(settings.watch_file_path)
    if sink_type == "command":
        return make_command_sink(settings.watch_command, timeout=settings.request_timeout, runner=command_runner)
    raise RuntimeError(f"Unknown sink type {sink_type!r}; use webhook, file, or command")


def _build_rule_sink(
    settings: Settings,
    rule: WatchRule,
    override: Sink | None,
    command_runner: Callable[[Sequence[str], bytes, float], tuple[int, str]] | None,
) -> Sink:
    """Resolve a rule's sink, but let an action-only rule (with no delivery target) deliver nothing."""
    try:
        return resolve_sink(settings, rule, override=override, command_runner=command_runner)
    except RuntimeError:
        if rule.actions:
            return _null_sink  # action-only rule: perform actions, no delivery
        raise


FLAG_ACTIONS = {"mark_read", "mark_unread", "star", "unstar", "label", "remove_label"}
MOVE_ACTIONS = {"archive", "trash", "move"}
FORWARD_ACTION = "forward"
ALLOWED_ACTIONS = FLAG_ACTIONS | MOVE_ACTIONS | {FORWARD_ACTION}


def _null_sink(event: Mapping[str, Any]) -> None:
    """Delivery target for action-only rules: run actions, deliver nothing."""
    return None


def _parse_actions(raw: Any, *, rule_name: str, source: str) -> tuple[dict[str, Any], ...]:
    """Validate a rule's ``actions`` list. Actions only apply to mail rules; perm-delete is refused."""
    if not raw:
        return ()
    if source != SOURCE_MAIL:
        raise ValueError(f"Rule {rule_name!r}: actions are only supported for mail rules")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"Rule {rule_name!r}: 'actions' must be a list")
    parsed: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError(f"Rule {rule_name!r}: each action must be an object")
        kind = item.get("type")
        if kind not in ALLOWED_ACTIONS:
            raise ValueError(
                f"Rule {rule_name!r}: unknown or disallowed action {kind!r}; allowed: {sorted(ALLOWED_ACTIONS)}"
            )
        action: dict[str, Any] = {"type": kind}
        if kind == "move":
            if not item.get("folder"):
                raise ValueError(f"Rule {rule_name!r}: move action requires a 'folder'")
            action["folder"] = str(item["folder"])
        elif kind in {"label", "remove_label"}:
            if not item.get("label"):
                raise ValueError(f"Rule {rule_name!r}: {kind} action requires a 'label'")
            action["label"] = str(item["label"])
        elif kind == FORWARD_ACTION:
            if not item.get("to"):
                raise ValueError(f"Rule {rule_name!r}: forward action requires a 'to' address")
            action["to"] = item["to"]
            if item.get("text"):
                action["text"] = str(item["text"])
        parsed.append(action)
    kinds = {action["type"] for action in parsed}
    if kinds & MOVE_ACTIONS and FORWARD_ACTION in kinds:
        raise ValueError(
            f"Rule {rule_name!r}: combining a move action (archive/trash/move) with forward is not "
            "supported, because the moved message gets a new UID that a retry cannot forward reliably. "
            "Split them into separate rules."
        )
    return tuple(parsed)


def _final_folder(client: BridgeMailClient, rule: WatchRule) -> str:
    """The folder a message ends up in after this rule's move actions (for a later forward read)."""
    folder = rule.folder
    for action in rule.actions:
        if action["type"] == "archive":
            folder = client.settings.archive_folder
        elif action["type"] == "trash":
            folder = client.settings.trash_folder
        elif action["type"] == "move":
            folder = action["folder"]
    return folder


def _run_flag_action(client: BridgeMailClient, action: dict[str, Any], uid: str, folder: str) -> None:
    kind = action["type"]
    if kind == "mark_read":
        client.mark_read(message_id=uid, folder=folder)
    elif kind == "mark_unread":
        client.mark_unread(message_id=uid, folder=folder)
    elif kind == "star":
        client.star_message(message_id=uid, folder=folder)
    elif kind == "unstar":
        client.unstar_message(message_id=uid, folder=folder)
    elif kind == "label":
        client.apply_label(message_id=uid, label=action["label"], folder=folder)
    elif kind == "remove_label":
        client.remove_label(message_id=uid, label=action["label"], folder=folder)


def _run_move_action(client: BridgeMailClient, action: dict[str, Any], uid: str, folder: str) -> None:
    kind = action["type"]
    if kind == "archive":
        client.archive_message(message_id=uid, folder=folder)
    elif kind == "trash":
        client.trash_message(message_id=uid, folder=folder)
    elif kind == "move":
        client.move_message(message_id=uid, destination_folder=action["folder"], folder=folder)


def process_event(client: BridgeMailClient | None, rule: WatchRule, event: Mapping[str, Any], sink: Sink) -> None:
    """Run a rule's actions and deliver the event.

    Order matters for at-least-once safety: flag/label actions (idempotent) run first, then the sink
    delivers, then moves run (so a delivery failure never moves the message out from under a retry),
    and forward runs last from the message's final folder (so a retry cannot easily double-send).
    """
    if not rule.actions or event.get("type") != EVENT_TYPE or client is None:
        sink(event)
        return
    uid = str(event["message"]["uid"])
    for action in rule.actions:
        if action["type"] in FLAG_ACTIONS:
            _run_flag_action(client, action, uid, rule.folder)
            logger.info("Ran action %r for rule %r on uid %s", action["type"], rule.name, uid)
    sink(event)
    for action in rule.actions:
        if action["type"] in MOVE_ACTIONS:
            _run_move_action(client, action, uid, rule.folder)
            logger.info("Ran action %r for rule %r on uid %s", action["type"], rule.name, uid)
    forwards = [action for action in rule.actions if action["type"] == FORWARD_ACTION]
    if forwards:
        final_folder = _final_folder(client, rule)
        for action in forwards:
            client.forward_mail(message_id=uid, to=action["to"], folder=final_folder, text=action.get("text", ""))
            logger.info("Ran forward action for rule %r on uid %s (at-least-once)", rule.name, uid)


def write_dead_letter(path: str | os.PathLike[str], record: Mapping[str, Any]) -> None:
    """Append one undeliverable event to the dead-letter JSONL file so the source can make progress."""
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(resolved, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


def _rewrite_dead_letter(path: Path, lines: list[str]) -> None:
    """Atomically replace the dead-letter file with ``lines`` (removing it when empty)."""
    if not lines:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_name(path.name + ".tmp")
    descriptor = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, ("\n".join(lines) + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    os.replace(tmp, path)


def replay_dead_letter(
    settings: Settings,
    *,
    path: str | os.PathLike[str] | None = None,
    rules: Sequence[WatchRule] | None = None,
    sink: Sink | None = None,
    command_runner: Callable[[Sequence[str], bytes, float], tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Re-deliver events parked in the dead-letter file through the configured sink.

    Events that deliver successfully are dropped; events that fail again are kept for a later retry,
    and any unparseable lines are preserved rather than discarded. Delivery uses the CURRENT sink
    configuration — a per-rule ``webhook_url`` from the original rule is not stored in the record, so
    a matching rule is looked up from the active rules config when available. Returns a summary dict.
    """
    resolved = Path(path).expanduser() if path else default_dead_letter_path(settings)
    if not resolved.exists():
        return {"path": str(resolved), "total": 0, "replayed": 0, "remaining": 0}

    rules = list(rules) if rules is not None else rules_from_config(settings)
    rules_by_name = {rule.name: rule for rule in rules}

    kept: list[str] = []
    replayed = 0
    total = 0
    for raw in resolved.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
            event = record["event"]
            rule_name = str(record.get("rule", ""))
            source = str(record.get("source", SOURCE_MAIL))
        except (json.JSONDecodeError, KeyError, TypeError):
            kept.append(raw)  # preserve data we cannot parse instead of dropping it
            continue
        total += 1
        rule = rules_by_name.get(rule_name) or WatchRule(name=rule_name or "dead-letter", source=source)
        try:
            target = resolve_sink(settings, rule, override=sink, command_runner=command_runner)
            target(event)
        except Exception as exc:
            logger.error("Replay failed for rule %r; keeping in dead-letter: %s", rule_name, redact_text(str(exc)))
            kept.append(raw)
        else:
            replayed += 1
            logger.info("Replayed %s event for rule %r", event.get("type"), rule_name)

    _rewrite_dead_letter(resolved, kept)
    return {"path": str(resolved), "total": total, "replayed": replayed, "remaining": len(kept)}


def poll_rule(
    client: BridgeMailClient | None,
    rule: WatchRule,
    store: CursorStore,
    *,
    simplelogin: SimpleLoginClient | None = None,
) -> dict[str, Any]:
    """Poll one rule and return its events plus the cursor to commit. Does not persist state itself.

    Persistence is the caller's job so that push delivery can hold the cursor when a target fails,
    giving at-least-once delivery instead of silently dropping events past an advanced cursor. Each
    outcome carries ``cursors`` (the commit position after each event, in order), ``commit_cursor``
    (the position to commit when there is nothing to deliver), and ``prior_cursor`` (the position
    before this poll), so the caller can advance generically regardless of source.
    """
    if rule.source == SOURCE_SIMPLELOGIN_ALIAS:
        return _poll_alias_rule(simplelogin, rule, store)
    return _poll_mail_rule(client, rule, store)


def _poll_mail_rule(client: BridgeMailClient | None, rule: WatchRule, store: CursorStore) -> dict[str, Any]:
    if client is None:
        raise RuntimeError("A Bridge mail client is required for mail rules")
    last_uid, uid_validity = store.get(rule.name)
    result = client.poll_folder(folder=rule.folder, last_uid=last_uid, uid_validity=uid_validity, **rule.criteria())
    if result.get("baseline"):
        logger.info("Baselined rule %r at UID %s (no backlog delivered)", rule.name, result["cursor_uid"])
    if result.get("reset"):
        logger.warning("UIDVALIDITY changed for rule %r; re-baselined at UID %s", rule.name, result["cursor_uid"])
    messages = result["messages"]
    return {
        "events": [build_event(rule.name, rule.folder, message) for message in messages],
        "cursors": [int(message["uid"]) for message in messages],
        "commit_cursor": result["cursor_uid"],
        "prior_cursor": last_uid,
        "uid_validity": result.get("uid_validity"),
        "baseline": result.get("baseline", False),
        "reset": result.get("reset", False),
        # Legacy keys kept for callers/tests that predate the generic cursor fields above.
        "cursor_uid": result["cursor_uid"],
        "prior_uid": last_uid,
    }


def _poll_alias_rule(simplelogin: SimpleLoginClient | None, rule: WatchRule, store: CursorStore) -> dict[str, Any]:
    if simplelogin is None:
        raise RuntimeError("A SimpleLogin client is required for simplelogin_alias rules")
    last_id, _ = store.get(rule.name)
    result = simplelogin.poll_aliases(last_id=last_id, query=rule.query, limit=rule.limit)
    if result.get("baseline"):
        logger.info("Baselined alias rule %r at alias id %s (no backlog delivered)", rule.name, result["cursor_id"])
    aliases = result["aliases"]
    return {
        "events": [build_alias_event(rule.name, alias) for alias in aliases],
        "cursors": [int(alias["id"]) for alias in aliases],
        "commit_cursor": result["cursor_id"],
        "prior_cursor": last_id,
        "uid_validity": None,
        "baseline": result.get("baseline", False),
        "reset": False,
    }


def rules_from_settings(settings: Settings) -> list[WatchRule]:
    unread = True if settings.watch_unread_only else None
    return [
        WatchRule(name=folder, folder=folder, unread=unread, limit=settings.watch_limit)
        for folder in settings.watch_folders
    ]


def load_rules_file(path: str | os.PathLike[str]) -> list[WatchRule]:
    """Load named triggers from a JSON rules file (a top-level array or ``{"rules": [...]}``)."""
    resolved = Path(path).expanduser()
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    if isinstance(raw, Mapping):
        entries = raw.get("rules", [])
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError("Rules file must be a JSON array or an object with a 'rules' array")
    rules = [_rule_from_mapping(entry) for entry in entries]
    if not rules:
        raise ValueError(f"Rules file {resolved} contains no rules")
    names = [rule.name for rule in rules]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"Duplicate rule names in rules file: {', '.join(duplicates)}")
    return rules


def _rule_from_mapping(entry: Any) -> WatchRule:
    if not isinstance(entry, Mapping):
        raise ValueError("Each rule must be a JSON object")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Each rule requires a non-empty string 'name'")
    source = str(entry.get("source", SOURCE_MAIL)).strip().lower() or SOURCE_MAIL
    if source not in KNOWN_SOURCES:
        raise ValueError(f"Rule {name!r} has unknown source {source!r}; use one of {sorted(KNOWN_SOURCES)}")
    return WatchRule(
        name=name,
        source=source,
        folder=str(entry.get("folder", "INBOX")),
        query=entry.get("query"),
        from_=entry.get("from", entry.get("from_")),
        to=entry.get("to"),
        subject=entry.get("subject"),
        unread=entry.get("unread"),
        starred=entry.get("starred"),
        limit=int(entry.get("limit", 50)),
        webhook_url=entry.get("webhook_url"),
        actions=_parse_actions(entry.get("actions"), rule_name=name, source=source),
    )


def rules_from_config(settings: Settings) -> list[WatchRule]:
    if settings.watch_rules_path:
        return load_rules_file(settings.watch_rules_path)
    return rules_from_settings(settings)


def run_watch(
    settings: Settings,
    *,
    rules: Sequence[WatchRule] | None = None,
    client: BridgeMailClient | None = None,
    simplelogin: SimpleLoginClient | None = None,
    store: CursorStore | None = None,
    once: bool = False,
    sink: Sink | None = None,
    command_runner: Callable[[Sequence[str], bytes, float], tuple[int, str]] | None = None,
    dry_run: bool = False,
) -> int:
    """Run the polling loop. ``sink`` overrides the configured delivery target. Returns events delivered.

    Delivery is at-least-once: a failing event holds the cursor and is retried next cycle. After
    ``watch_dead_letter_max_attempts`` consecutive failing cycles on the same event, that event is
    written to the dead-letter file and the cursor advances past it, so one poison event can never
    stall a source forever.
    """
    if dry_run:
        once = True
    rules = list(rules) if rules is not None else rules_from_config(settings)
    if not rules:
        raise RuntimeError("No watch rules configured")

    if any(rule.source in (SOURCE_MAIL, "") for rule in rules):
        settings.require_bridge()
        client = client or BridgeMailClient(settings)
    if any(rule.source == SOURCE_SIMPLELOGIN_ALIAS for rule in rules):
        settings.require_simplelogin()
        simplelogin = simplelogin or SimpleLoginClient(settings)

    store = store or CursorStore.load(default_state_path(settings))
    sinks = {} if dry_run else {rule.name: _build_rule_sink(settings, rule, sink, command_runner) for rule in rules}
    action_client = (
        GuardedClient(client, OperationGuard(settings, enforce_auth=False), MAIL_POLICIES)
        if client is not None
        else None
    )

    dead_letter_path = default_dead_letter_path(settings)
    max_attempts = max(settings.watch_dead_letter_max_attempts, 1)

    total = 0
    interval = max(settings.watch_poll_interval, 1.0)
    if dry_run:
        logger.info("Dry-run watching %d source(s); cursors, sinks, and actions will not be changed", len(rules))
    else:
        logger.info("Watching %d source(s) every %.0fs", len(rules), interval)
    while True:
        for rule in rules:
            try:
                outcome = poll_rule(client, rule, store, simplelogin=simplelogin)
            except Exception as exc:  # keep the loop alive; one bad poll should not stop the watcher
                logger.error("Poll failed for rule %r: %s", rule.name, redact_text(str(exc)))
                continue

            events = outcome["events"]
            if dry_run:
                total += _log_dry_run_rule(rule, outcome)
                continue
            # For baseline, reset, or an empty poll there is nothing to deliver: commit the head.
            if not events:
                store.set(rule.name, cursor_uid=outcome["commit_cursor"], uid_validity=outcome["uid_validity"])
                store.clear_failure(rule.name)
                store.save()
                continue

            total += _deliver_rule_events(
                rule=rule,
                outcome=outcome,
                sink=sinks[rule.name],
                client=action_client,
                store=store,
                dead_letter_path=dead_letter_path,
                max_attempts=max_attempts,
            )
            store.save()
        if once:
            return total
        _wait_between_cycles(settings, client, rules, interval)


def _log_dry_run_rule(rule: WatchRule, outcome: Mapping[str, Any]) -> int:
    """Log what a rule would emit or mutate without delivering, acting, or advancing cursors."""
    events = outcome["events"]
    if not events:
        logger.info(
            "Dry run: rule %r would emit 0 event(s); cursor would advance to %s",
            rule.name,
            outcome["commit_cursor"],
        )
        return 0
    for event in events:
        uid = event.get("message", {}).get("uid") if isinstance(event.get("message"), Mapping) else None
        alias_id = event.get("alias", {}).get("id") if isinstance(event.get("alias"), Mapping) else None
        target = f"uid {uid}" if uid is not None else f"alias {alias_id}" if alias_id is not None else "item"
        logger.info("Dry run: rule %r would emit %s for %s", rule.name, event.get("type"), target)
        for action in rule.actions:
            logger.info("Dry run: rule %r would run action %s for %s", rule.name, action["type"], target)
    logger.info(
        "Dry run: rule %r would advance cursor from %s to %s",
        rule.name,
        outcome["prior_cursor"],
        outcome["cursors"][-1],
    )
    return len(events)


def _wait_between_cycles(
    settings: Settings, client: BridgeMailClient | None, rules: Sequence[WatchRule], interval: float
) -> None:
    """Sleep between poll cycles, or block on IMAP IDLE for near-instant mail triggers when enabled."""
    idle_folder = next((rule.folder for rule in rules if rule.source in (SOURCE_MAIL, "")), None)
    if settings.watch_idle and client is not None and idle_folder is not None:
        if client.idle_wait(folder=idle_folder, timeout=interval):
            return  # IDLE waited (activity or its own timeout); poll again now
    time.sleep(interval)


def _deliver_rule_events(
    *,
    rule: WatchRule,
    outcome: Mapping[str, Any],
    sink: Sink,
    client: BridgeMailClient | None,
    store: CursorStore,
    dead_letter_path: str | os.PathLike[str],
    max_attempts: int,
) -> int:
    """Deliver a rule's events in order, advancing the cursor only past accepted or dead-lettered events."""
    events = outcome["events"]
    cursors = outcome["cursors"]
    uid_validity = outcome["uid_validity"]
    committed = outcome["prior_cursor"]
    delivered = 0
    index = 0
    while index < len(events):
        event = events[index]
        this_cursor = cursors[index]
        try:
            process_event(client, rule, event, sink)
        except OperationBlocked as exc:
            store.clear_failure(rule.name)
            logger.error(
                "Action blocked by safety policy for rule %r; holding cursor at %s: %s",
                rule.name,
                committed,
                redact_text(str(exc)),
            )
            break
        except Exception as exc:
            fail_cursor, fail_count = store.get_failure(rule.name)
            fail_count = fail_count + 1 if fail_cursor == this_cursor else 1
            if fail_count >= max_attempts:
                write_dead_letter(
                    dead_letter_path,
                    {
                        "rule": rule.name,
                        "source": rule.source,
                        "event": event,
                        "attempts": fail_count,
                        "error": redact_text(str(exc)),
                        "dead_lettered_at": int(time.time()),
                    },
                )
                logger.error(
                    "Dead-lettered %s event for rule %r after %d cycle(s); advancing past it: %s",
                    event.get("type"),
                    rule.name,
                    fail_count,
                    redact_text(str(exc)),
                )
                committed = this_cursor
                store.clear_failure(rule.name)
                index += 1
                continue
            store.set_failure(rule.name, fail_cursor=this_cursor, fail_count=fail_count)
            logger.error(
                "Delivery failed for rule %r; holding cursor at %s (cycle %d/%d): %s",
                rule.name,
                committed,
                fail_count,
                max_attempts,
                redact_text(str(exc)),
            )
            break
        else:
            delivered += 1
            committed = this_cursor
            store.clear_failure(rule.name)
            index += 1
    store.set(rule.name, cursor_uid=committed, uid_validity=uid_validity)
    return delivered


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="proton-workflow-watch",
        description="Watch Proton Mail and SimpleLogin through Bridge and push new-activity events to a target.",
    )
    parser.add_argument("--env-file", help="Optional .env file to load before reading settings.")
    parser.add_argument("--folder", action="append", dest="folders", help="Folder to watch (repeatable).")
    parser.add_argument("--rules", help="JSON rules file with named triggers (overrides --folder).")
    parser.add_argument("--webhook-url", help="Override PROTON_MCP_WATCH_WEBHOOK_URL.")
    parser.add_argument("--sink", choices=["webhook", "file", "command"], help="Delivery target (default webhook).")
    parser.add_argument("--file", dest="file_path", help="Path for the JSONL file sink.")
    parser.add_argument("--command", help="Command to pipe each event to (stdin) for the command sink.")
    parser.add_argument("--dead-letter", dest="dead_letter", help="Dead-letter JSONL file path.")
    parser.add_argument(
        "--replay-dead-letter",
        dest="replay_dead_letter",
        action="store_true",
        help="Re-deliver parked dead-letter events through the configured sink, then exit.",
    )
    parser.add_argument(
        "--dead-letter-max-attempts",
        dest="dead_letter_max_attempts",
        type=int,
        help="Failed delivery cycles on one event before it is dead-lettered.",
    )
    parser.add_argument("--interval", type=float, help="Seconds between polls.")
    parser.add_argument(
        "--idle",
        action="store_true",
        help="Use IMAP IDLE for near-instant mail triggers (falls back to --interval polling).",
    )
    parser.add_argument("--unread-only", action="store_true", help="Only emit events for unread messages.")
    parser.add_argument("--once", action="store_true", help="Poll a single time and exit (useful for cron).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Poll once and log what would emit or act without delivering events or advancing cursors.",
    )
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
    if args.rules:
        overrides["watch_rules_path"] = args.rules
    if args.webhook_url:
        overrides["watch_webhook_url"] = args.webhook_url
    if args.sink:
        overrides["watch_sink"] = args.sink
    if args.file_path:
        overrides["watch_file_path"] = args.file_path
    if args.command:
        overrides["watch_command"] = args.command
    if args.dead_letter:
        overrides["watch_dead_letter_path"] = args.dead_letter
    if args.dead_letter_max_attempts is not None:
        overrides["watch_dead_letter_max_attempts"] = args.dead_letter_max_attempts
    if args.interval is not None:
        overrides["watch_poll_interval"] = args.interval
    if args.idle:
        overrides["watch_idle"] = True
    if args.unread_only:
        overrides["watch_unread_only"] = True
    if args.state_path:
        overrides["watch_state_path"] = args.state_path
    if overrides:
        settings = replace_settings(settings, **overrides)

    if args.replay_dead_letter:
        summary = replay_dead_letter(settings)
        logger.info(
            "Replayed %d of %d dead-letter event(s); %d remaining in %s",
            summary["replayed"],
            summary["total"],
            summary["remaining"],
            summary["path"],
        )
        return

    try:
        count = run_watch(settings, once=args.once or args.dry_run, dry_run=args.dry_run)
        if args.dry_run:
            logger.info("Dry run found %d event(s) that would be delivered", count)
        elif args.once:
            logger.info("Delivered %d event(s)", count)
    except KeyboardInterrupt:
        pass


def replace_settings(settings: Settings, **overrides: Any) -> Settings:
    from dataclasses import replace

    return replace(settings, **overrides)


if __name__ == "__main__":  # pragma: no cover
    main()

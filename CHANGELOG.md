# Changelog

All notable changes to this project are recorded here. Versions follow [semantic versioning](https://semver.org/).

## 1.3.0 - 2026-07-02

### Added

- `simplelogin_get_alias_options`: list the custom-alias suffixes SimpleLogin offers,
  including the `signed_suffix` that `simplelogin_create_custom_alias` requires. Custom
  aliases were previously impossible to create through the connector alone.
- A wall-clock deadline on every operation (`PROTON_MCP_OPERATION_DEADLINE`, default 90s).
  A Bridge session that stops responding mid-command now returns a clear error instead of
  hanging the tool call indefinitely.
- Blocked operations (read-only mode, sends disabled, allowed-actions) are written to the
  audit log with the reason they were refused.

### Changed

- Failing to reach Bridge now reports the address and asks "is Bridge running?" instead of
  surfacing a raw socket error.
- The live acceptance suite runs its safety checks concurrently and polls more tightly,
  cutting a full run from about 12 minutes to about 6.

## 1.2.1 - 2026-07-01

### Fixed

- Synchronous tools now run in worker threads instead of on the event loop, so one stalled
  Bridge IMAP call can no longer freeze the whole server for every session.
- JWKS and OIDC discovery fetches send an explicit `User-Agent`; CDNs and WAFs that block
  urllib's default agent were silently rejecting every OAuth token with 401. Failed token
  verifications are now logged with the reason instead of being swallowed.
- Message fetches retry briefly when Bridge answers an OK FETCH without the message body
  (a transient Bridge quirk under load), and report a clear "moved or deleted" error when
  a stale UID is fetched after a message left the folder.

All three were found by the new live acceptance harness (`scripts/live_acceptance.py`), which
exercises all 67 tools against a real Bridge, SimpleLogin, watcher sinks, safety modes, and a
hosted OAuth deployment.

## 1.2.0 - 2026-07-01

### Added

- Proton label tools, header inspection, richer search filters, draft replies/forwards, and
  List-Unsubscribe handling.
- Rule actions for the watcher, including mark read/unread, star/unstar, label/remove label,
  archive/trash/move, and forward.
- IMAP IDLE support for lower-latency watcher polling when Bridge supports it.
- Read-only mode, send disablement, allowed-action guardrails, and MCP tool annotations.
- Dry-run previews for send/reply/forward, bulk operations, permanent delete, empty-folder operations,
  and watcher rules.
- Dependency-free outbound HTML sanitization with `html_sanitized` result markers and a
  `trusted_html` opt-out for caller-controlled HTML.
- `content_trust: "untrusted"` markers on read/search message payloads.
- Post-operation verification for message flags and label application.

### Changed

- Reworked the README around clearer setup, capabilities, automations, and security boundaries.

### Fixed

- Watcher rules that combine a move action with `forward` are rejected at parse time because retries
  cannot reliably forward a message after its UID changes.

## 1.1.0 - 2026-07-01

Turns the watcher into a real workflow layer: more event sources, flexible delivery, and reliable
forward progress.

### Added

- **SimpleLogin alias events.** A second event source (`alias.created`) alongside `mail.received`,
  with a cursor on the highest alias id. Add it as a watcher source or pull it with the new
  `poll_aliases` MCP tool.
- **Rules file.** Point `PROTON_MCP_WATCH_RULES` (or `--rules`) at a JSON file to run several named
  triggers at once, each with its own source, filter, and optional `webhook_url`.
- **Delivery targets.** Besides the default webhook, the watcher can append events to a JSONL file
  (`--sink file`) or pipe them to a command's stdin (`--sink command`).
- **Dead-letter and replay.** After a configurable number of failed cycles, a stuck event is written
  to a dead-letter file and the cursor advances so one bad event can't stall a source. Re-deliver
  parked events later with `--replay-dead-letter`.
- **Trust model** documented in [docs/WATCH.md](docs/WATCH.md).

### Fixed

- Alias polling no longer stops early on SimpleLogin's out-of-order list (pinned and recently active
  aliases float to the top), which previously let new aliases on later pages be missed.

## 1.0.0

Initial release: MCP tools for Proton Mail through Bridge and SimpleLogin, local stdio / private
HTTP / hosted OAuth transports, and the first trigger layer (`poll_mailbox` plus a webhook watcher).

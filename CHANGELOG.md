# Changelog

All notable changes to this project are recorded here. Versions follow [semantic versioning](https://semver.org/).

## Unreleased

### Added

- Dry-run previews for send/reply/forward, bulk operations, permanent delete, empty-folder operations,
  and watcher rules.
- Dependency-free outbound HTML sanitization with `html_sanitized` result markers and a
  `trusted_html` opt-out for caller-controlled HTML.
- `content_trust: "untrusted"` markers on read/search message payloads.
- Post-operation verification for message flags and label application.

## 1.1.0 — 2026-07-01

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

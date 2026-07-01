# Triggers and webhooks

Most Proton automation tools are request/response only: an agent has to ask "any new mail?" for
anything to happen. This connector adds a **trigger layer** so new activity can drive the rest of
your stack. Because Proton and SimpleLogin have no public push API, the watcher polls on an interval
and emits one event per new item. There are two ways to consume those events:

- **Push** — `proton-workflow-watch` runs a loop and delivers each event to a target: a webhook
  (n8n, Zapier, Make, a serverless function, your own service), an appended JSONL file, or an
  external command.
- **Pull** — the `poll_mailbox` MCP tool returns everything new since the last call, so an agent can
  build "when new mail matching X arrives, do Y" without a background process.

Both share the same cursor logic, so they never replay history and never miss activity across
restarts.

## Event sources

| Source (`source`) | Event type | Cursor | Filter |
| --- | --- | --- | --- |
| `mail` (default) | `mail.received` | IMAP UID (UIDVALIDITY-aware) | `folder`, `from`, `to`, `subject`, `unread`, `starred` |
| `simplelogin_alias` | `alias.created` | maximum SimpleLogin alias id | `query` (alias email substring) |

Both sources baseline on first run (record the current head, emit nothing) so you are never flooded
with backlog. Alias ids are monotonic, so a newly created alias always has an id above the cursor;
the watcher reads the newest alias page and pages back until it passes the cursor, so a burst of new
aliases is not missed. Configure the SimpleLogin source with `SIMPLELOGIN_API_KEY`.

**Mail flag/move events are deferred.** Detecting reads/stars/moves over plain IMAP polling would
require snapshotting every message's flags each cycle (and, for moves, correlating a UID vanishing in
one folder with a new UID appearing in another). That is O(mailbox) per poll and needs per-message
state the cursor model does not keep; doing it cheaply needs `CONDSTORE`/`QRESYNC`, whose support
through Bridge is not guaranteed. Until that is validated, only new-item events (`mail.received`,
`alias.created`) are emitted.

## How cursors work

Each trigger tracks a cursor: the last IMAP UID it has seen, plus the folder's `UIDVALIDITY`.

- **First run baselines.** With no stored cursor, the watcher records the current mailbox head and
  emits nothing. You are never flooded with your entire backlog when you start watching.
- **Subsequent runs** search for UIDs above the cursor that match your filter, emit those, and
  advance the cursor past the last delivered message.
- **`UIDVALIDITY` change** (Bridge re-created the mailbox) re-baselines instead of replaying, so a
  Bridge resync does not spam your webhook.

Push cursors live in a JSON state file (`$XDG_STATE_HOME/proton-workflow-connector/watch-state.json`
by default, override with `PROTON_MCP_WATCH_STATE`). The `poll_mailbox` tool uses the same file.

## Push: run the watcher

```bash
proton-workflow-watch \
  --env-file ~/.config/proton-workflow-connector/env \
  --folder INBOX \
  --webhook-url https://example.com/hooks/proton \
  --interval 60
```

Preview a watcher run without delivering events, running actions, or advancing cursors:

```bash
proton-workflow-watch \
  --env-file ~/.config/proton-workflow-connector/env \
  --rules ./rules.json \
  --dry-run
```

Everything is also configurable through the environment (see `.env.example`):

| Variable | Meaning |
| --- | --- |
| `PROTON_MCP_WATCH_WEBHOOK_URL` | Where events are POSTed. Required for push. |
| `PROTON_MCP_WATCH_WEBHOOK_SECRET` | Optional HMAC key for the `X-Proton-Signature` header. |
| `PROTON_MCP_WATCH_FOLDERS` | Comma-separated folders to watch. Defaults to `INBOX`. |
| `PROTON_MCP_WATCH_INTERVAL` | Seconds between polls. |
| `PROTON_MCP_WATCH_LIMIT` | Max messages emitted per folder per poll. |
| `PROTON_MCP_WATCH_UNREAD_ONLY` | Only emit events for unread messages. |
| `PROTON_MCP_WATCH_MAX_RETRIES` | Webhook attempts per event within one cycle before the cursor is held. |
| `PROTON_MCP_WATCH_RETRY_BACKOFF` | Base seconds for exponential backoff between retries. |
| `PROTON_MCP_WATCH_STATE` | Cursor state file location. |
| `PROTON_MCP_WATCH_RULES` | JSON rules file with named triggers (overrides `PROTON_MCP_WATCH_FOLDERS`). |
| `PROTON_MCP_WATCH_SINK` | Delivery target: `webhook` (default), `file`, or `command`. |
| `PROTON_MCP_WATCH_FILE` | Path for the file sink. |
| `PROTON_MCP_WATCH_COMMAND` | Command for the command sink (event JSON is piped to stdin). |
| `PROTON_MCP_WATCH_DEAD_LETTER` | Dead-letter JSONL file (defaults next to the state file). |
| `PROTON_MCP_WATCH_DEAD_LETTER_MAX_ATTEMPTS` | Failed cycles on one event before it is dead-lettered. |

## Rules file: multiple named triggers

For anything beyond "watch these folders with one filter", point `PROTON_MCP_WATCH_RULES` (or
`--rules`) at a JSON file. Each rule is independent: its own source, filter, and optional
`webhook_url` that overrides the global delivery target (so one watcher can fan out to several
stacks).

```json
{
  "rules": [
    {"name": "invoices", "source": "mail", "folder": "INBOX", "from": "billing@vendor.example", "unread": true},
    {"name": "urgent", "source": "mail", "folder": "INBOX", "subject": "URGENT", "webhook_url": "https://ops.example/hooks/page"},
    {"name": "new-aliases", "source": "simplelogin_alias", "query": "shop"}
  ]
}
```

A top-level array (without the `rules` wrapper) is also accepted. Rule names must be unique — they
key the persisted cursor, so reusing a name would share a cursor.

## Rule actions: act, don't just notify

A mail rule can carry an `actions` list. When it matches a message, the watcher runs those actions
on it through the same IMAP client the tools use — so the connector becomes the server-side filters
Bridge doesn't expose. A rule can act, notify (a sink), or both; a rule with actions and no delivery
target just acts.

```json
{
  "rules": [
    { "name": "newsletters", "source": "mail", "folder": "INBOX", "from": "substack.com",
      "actions": [ {"type": "label", "label": "News"}, {"type": "mark_read"}, {"type": "archive"} ] },
    { "name": "receipts", "source": "mail", "folder": "INBOX", "subject": "receipt",
      "webhook_url": "https://example.com/hooks/receipts",
      "actions": [ {"type": "forward", "to": "books@example.com"} ] }
  ]
}
```

Available actions: `mark_read`, `mark_unread`, `star`, `unstar`, `label`, `remove_label`, `archive`,
`trash`, `move` (needs `folder`), and `forward` (needs `to`, optional `text`). `label`/`remove_label`
need a `label`. Permanent deletion is intentionally not available as an auto-action.

Use `--dry-run` after editing rules. It polls once and logs the events and actions that would fire,
but it does not deliver to sinks, run actions, or write cursor state.

Ordering is chosen for reliability under at-least-once delivery: flag/label actions run first, then
the sink delivers, then moves run (so a delivery failure can never move the message out from under a
retry), and `forward` runs last from the message's final folder. **`forward` is at-least-once** — if
a later step fails and the event retries, a forward can fire twice, so keep it for cases where a
duplicate is tolerable. Only one move-type action (`archive`/`trash`/`move`) per rule.

## Low-latency triggers with IMAP IDLE

By default the watcher polls every `--interval` seconds. Add `--idle` (or `PROTON_MCP_WATCH_IDLE=true`)
and it uses IMAP IDLE on the primary mail folder to wake within moments of new mail instead of waiting
for the next poll. It falls back to interval polling if the server doesn't support IDLE. Alias sources
and other folders are still polled at the interval on each wake, so IDLE mainly cuts INBOX latency.

## Delivery targets (sinks)

`PROTON_MCP_WATCH_SINK` (or `--sink`) selects where events go. Webhook stays the default.

- **`webhook`** — POSTs JSON to `PROTON_MCP_WATCH_WEBHOOK_URL` (or a rule's `webhook_url`), with the
  optional HMAC signature. Retried with backoff; see *Delivery guarantees* below.
- **`file`** — appends each event as one JSON line to `PROTON_MCP_WATCH_FILE`. Good for piping into
  `tail -f`, a log shipper, or batch jobs.
- **`command`** — runs `PROTON_MCP_WATCH_COMMAND` once per event with the event JSON on stdin. A
  non-zero exit is treated as a failed delivery (so retry/dead-letter logic applies). The command is
  split with shell-like tokenization and executed directly (no shell), e.g.
  `--command "/usr/local/bin/notify --queue proton"`.

A per-rule `webhook_url` always delivers that rule via webhook, even when the global sink is `file`
or `command`.

## Dead-letter and forward progress

Delivery is at-least-once, so by default a failing event holds the cursor and is retried every cycle.
To keep one poison event from stalling a source forever, after
`PROTON_MCP_WATCH_DEAD_LETTER_MAX_ATTEMPTS` consecutive failing **cycles** on the same event, the
watcher appends that event (with the error and attempt count) to the dead-letter JSONL file and
advances the cursor past it. The failure counter is persisted in the cursor state file, so it counts
correctly across restarts and `--once` cron invocations. Inspect or replay dead-lettered events from
that file later.

To retry parked events once the receiver is healthy again:

```bash
proton-workflow-watch --env-file … --sink webhook --replay-dead-letter
```

Replay re-delivers each event through the configured sink; events that succeed are dropped and any
that still fail stay in the file for a later attempt. Delivery uses the *current* sink config (a
rule's original per-rule `webhook_url` is not stored in the dead-letter record).

Use `--once` to poll a single time and exit, which suits `cron` or a systemd timer. For a
long-running service, see [`examples/systemd/proton-workflow-watch.service`](../examples/systemd/proton-workflow-watch.service).

## Event payload

Each event is a JSON POST with `Content-Type: application/json` and an `X-Proton-Event: mail.received`
header:

```json
{
  "type": "mail.received",
  "rule": "INBOX",
  "folder": "INBOX",
  "message": {
    "uid": "1841",
    "subject": "Invoice #204",
    "from_": "billing@vendor.example",
    "to": "you@proton.me",
    "date": "Tue, 01 Jul 2026 09:12:00 +0000",
    "message_id": "<...@vendor.example>",
    "flags": ["\\Seen"],
    "content_trust": "untrusted"
  },
  "timestamp": 1751360000
}
```

The event carries message metadata only. Use the `read_mail` tool (or another IMAP client) with the
`uid` to fetch the full body and attachments when you need them.

An `alias.created` event carries the new alias's metadata and an `X-Proton-Event: alias.created`
header:

```json
{
  "type": "alias.created",
  "rule": "new-aliases",
  "alias": {
    "id": 84213,
    "email": "shop.abc123@aliases.example",
    "enabled": true,
    "note": null,
    "creation_date": "2026-07-01 09:12:00+00:00"
  },
  "timestamp": 1751360000
}
```

## Verifying the signature

When `PROTON_MCP_WATCH_WEBHOOK_SECRET` is set, every request includes
`X-Proton-Signature: sha256=<hex>`, an HMAC-SHA256 of the raw request body. Verify it before trusting
the payload:

```python
import hashlib
import hmac

def verify(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

Compute the HMAC over the exact bytes you received, before any JSON re-serialization.

## Delivery guarantees

Push delivery is **at-least-once**. Transient failures (a network error, HTTP 429, or any 5xx) are
retried with exponential backoff up to `PROTON_MCP_WATCH_MAX_RETRIES`. If an event still cannot be
delivered, the watcher holds the cursor at the last accepted message and retries the rest on the next
poll instead of advancing past — so a webhook outage delays events rather than dropping them. A 4xx
other than 429 is treated as a configuration error and is not retried.

Because delivery is at-least-once, a receiver can see the same event twice after a partial failure.
Deduplicate on `message.message_id` (or `folder` + `message.uid`) if your workflow is not idempotent.

## Pull: the `poll_mailbox` tool

Agents that only need triggers on demand can skip the background process:

```
poll_mailbox(folder="INBOX", unread=True, sender="billing@vendor.example", cursor_name="invoices")
```

The first call baselines and returns no messages; each later call returns only mail that arrived
since the previous call for that `cursor_name`. This is the building block for agent-driven
automations ("check for new invoices, file them, reply") without polling loops in your own code.

The `poll_aliases` tool is the SimpleLogin counterpart:

```
poll_aliases(query="shop", cursor_name="shop-aliases")
```

It baselines to the current highest alias id on first call, then returns only aliases created since
the previous call for that `cursor_name` — the pull-side equivalent of the `simplelogin_alias`
watcher source.

## Security and trust model

The watcher runs on your machine with your Bridge credentials, and the command sink can run a
program per event, so a few boundaries are worth stating plainly:

- **The command sink runs a program you choose.** It comes only from `PROTON_MCP_WATCH_COMMAND` or
  `--command` — never from a rules file. The event JSON is written to that program's standard input,
  not spliced into its command line, so a crafted email subject or alias name can't inject arguments.
  It runs without a shell, so there's no shell expansion to worry about.
- **A rules file is trusted configuration.** Its per-rule `webhook_url` decides where events go and
  its `actions` move, label, or forward your mail with your account's access, so treat a rules file
  like a config secret: don't load one from a source you don't control.
- **Events carry attacker-influenced content.** Anyone can email you a subject line or create an
  alias name, and that text ends up in the event payload. Mail message payloads include
  `content_trust: "untrusted"` for this reason. Whatever consumes the event — your webhook receiver,
  log pipeline, or command — should treat event fields as data, not instructions.

## Limitations

- **Polling, not push.** Proton exposes no webhooks, so latency is bounded by your interval. Lower
  intervals cost more IMAP round-trips against Bridge.
- **Bridge must be running.** The watcher connects to the same local Bridge IMAP endpoint as every
  other tool here.
- **Metadata only in events.** Bodies stay in Proton until you fetch them by UID, keeping payloads
  small and reducing how much plaintext leaves your machine.

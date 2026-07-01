# Triggers and webhooks

Most Proton automation tools are request/response only: an agent has to ask "any new mail?" for
anything to happen. This connector adds a **trigger layer** so new mail can drive the rest of your
stack. Because Proton has no public push API, the watcher polls Bridge over IMAP on an interval and
emits one event per new message. There are two ways to consume those events:

- **Push** — `proton-workflow-watch` runs a loop and POSTs each event to a webhook (n8n, Zapier,
  Make, a serverless function, your own service).
- **Pull** — the `poll_mailbox` MCP tool returns everything new since the last call, so an agent can
  build "when new mail matching X arrives, do Y" without a background process.

Both share the same cursor logic, so they never replay history and never miss mail across restarts.

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

Everything is also configurable through the environment (see `.env.example`):

| Variable | Meaning |
| --- | --- |
| `PROTON_MCP_WATCH_WEBHOOK_URL` | Where events are POSTed. Required for push. |
| `PROTON_MCP_WATCH_WEBHOOK_SECRET` | Optional HMAC key for the `X-Proton-Signature` header. |
| `PROTON_MCP_WATCH_FOLDERS` | Comma-separated folders to watch. Defaults to `INBOX`. |
| `PROTON_MCP_WATCH_INTERVAL` | Seconds between polls. |
| `PROTON_MCP_WATCH_LIMIT` | Max messages emitted per folder per poll. |
| `PROTON_MCP_WATCH_UNREAD_ONLY` | Only emit events for unread messages. |
| `PROTON_MCP_WATCH_MAX_RETRIES` | Delivery attempts per event before the cursor is held. |
| `PROTON_MCP_WATCH_RETRY_BACKOFF` | Base seconds for exponential backoff between retries. |
| `PROTON_MCP_WATCH_STATE` | Cursor state file location. |

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
    "flags": ["\\Seen"]
  },
  "timestamp": 1751360000
}
```

The event carries message metadata only. Use the `read_mail` tool (or another IMAP client) with the
`uid` to fetch the full body and attachments when you need them.

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

## Limitations

- **Polling, not push.** Proton exposes no webhooks, so latency is bounded by your interval. Lower
  intervals cost more IMAP round-trips against Bridge.
- **Bridge must be running.** The watcher connects to the same local Bridge IMAP endpoint as every
  other tool here.
- **Metadata only in events.** Bodies stay in Proton until you fetch them by UID, keeping payloads
  small and reducing how much plaintext leaves your machine.

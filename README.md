# Proton Workflow Connector

[![CI](https://github.com/NtrpyDev/proton-workflow-connector/actions/workflows/ci.yml/badge.svg)](https://github.com/NtrpyDev/proton-workflow-connector/actions/workflows/ci.yml)
[![Secret scan](https://github.com/NtrpyDev/proton-workflow-connector/actions/workflows/secret-scan.yml/badge.svg)](https://github.com/NtrpyDev/proton-workflow-connector/actions/workflows/secret-scan.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Proton Workflow Connector (PWC) is a self-hosted MCP connector for reading, sending, organizing, and automating Proton Mail through Proton Mail Bridge.
It works with any MCP client or local agent runtime that can launch a stdio server or call a Streamable HTTP MCP endpoint.
SimpleLogin support is optional.

This is an unofficial project. It is not affiliated with, endorsed by, or sponsored by Proton AG.
The connector uses Bridge-generated IMAP/SMTP credentials. It does not accept Proton account passwords or store Proton login sessions.

[Quick start](#quick-start) · [Example workflows](#example-workflows) · [Capabilities](#capabilities) · [Automations](#automations) · [Security](#security-model) · [Documentation](#documentation) · [Buy Me a Coffee](https://buymeacoffee.com/ntrpydev)

## Why use it?

Use one local server for the mail work agents usually have to piece together:

- Search, read, send, reply, forward, draft, label, archive, and download attachments through 53 Proton Mail tools.
- Require confirmation for destructive tools, preview sends and destructive operations with `dry_run`, sanitize outbound HTML by default, bound bulk operations, and allow only configured sender addresses.
- React to new mail or SimpleLogin aliases through MCP polling or a background watcher that can call a webhook, append JSONL, or run a command.
- Add 13 SimpleLogin tools when you want alias, contact, and mailbox management.
- Connect over stdio, localhost Streamable HTTP, or OAuth-protected hosted HTTP.

The server exposes 67 tools in total. See the [complete tool reference](docs/TOOLS.md).

## How it works

PWC is an MCP server. Your client starts it over stdio or connects to it over Streamable HTTP.

The connector talks to Proton Mail Bridge over local IMAP/SMTP. Bridge must be installed, signed in, and running on the same machine as the connector. One connector process serves one Bridge account.

SimpleLogin is optional. If `SIMPLELOGIN_API_KEY` is set, PWC adds alias, contact, mailbox, and new-alias polling tools. If it is unset, the Proton Mail tools still work.

For automation, run `proton-workflow-watch` or call the polling tools from your MCP client. Watcher output can go to a webhook, a JSONL file, or a command.

## Quick start

### Prerequisites

- Python 3.11 or newer
- [Proton Mail Bridge](https://proton.me/mail/bridge), signed in and running
- The IMAP/SMTP host, ports, username, and generated password shown by Bridge
- A SimpleLogin API key only if you want the optional SimpleLogin tools

### 1. Install

```bash
git clone https://github.com/NtrpyDev/proton-workflow-connector.git
cd proton-workflow-connector
python -m venv .venv
source .venv/bin/activate
python -m pip install .
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1`.

### 2. Configure Bridge credentials

Keep the environment file outside the repository:

```bash
mkdir -p ~/.config/proton-workflow-connector
cp .env.example ~/.config/proton-workflow-connector/env
chmod 600 ~/.config/proton-workflow-connector/env
```

Edit the copied file with the connection details shown in Proton Mail Bridge. Set `PROTON_BRIDGE_EMAIL` to the default sender and list every permitted sender address in `PROTON_BRIDGE_SENDER_ADDRESSES`.

### 3. Run the connector

```bash
proton-workflow-connector --transport stdio \
  --env-file ~/.config/proton-workflow-connector/env
```

### 4. Connect an MCP client

Any MCP client or local agent runtime can use PWC as long as it supports stdio or Streamable HTTP:

- stdio: run `proton-workflow-connector --transport stdio --env-file ...`
- HTTP: run the connector with `--transport streamable-http` and point the client at `/mcp`

Claude Code stdio example:

```bash
claude mcp add --transport stdio --scope user proton-workflow \
  -- "$PWD/.venv/bin/proton-workflow-connector" --transport stdio \
  --env-file ~/.config/proton-workflow-connector/env
```

Codex stdio example:

```toml
[mcp_servers.proton_workflow]
command = "/absolute/path/to/repo/.venv/bin/proton-workflow-connector"
args = [
  "--transport", "stdio",
  "--env-file", "/absolute/path/to/private/proton-workflow-connector-env"
]
default_tools_approval_mode = "prompt"
startup_timeout_sec = 20
tool_timeout_sec = 120
```

Run `server_status` after connecting. It checks IMAP, SMTP, SimpleLogin, OAuth configuration, and the server version without returning secrets.
Then run `list_folders`; Bridge folder names vary by account and version.

See [MCP client setup](docs/CLIENTS.md) for generic client configuration, localhost HTTP, hosted Streamable HTTP, Codex, Claude Code, and the plugin wrapper.

## Example workflows

Once connected, ask your MCP client to:

- "List my mail folders and show the unread count for each."
- "Find unread invoices from the last 30 days without marking them read."
- "Draft a reply to the latest message in this thread, but do not send it."
- "Download the PDF attachments from this message."
- "Archive these message UIDs after showing me the exact list."
- "Create a SimpleLogin alias for shopping and add a contact for this merchant."
- "Poll for new mail from `billing@vendor.example` using the `invoices` cursor."

The connector returns structured data to the MCP client. The client decides how to present it and when to request approval for actions.

## Capabilities

| Area | Included operations |
| --- | --- |
| Search and reading | Search one folder or all selectable folders, read messages and threads, inspect headers, and download attachments |
| Sending and drafts | Send, reply, reply-all, forward, create and update drafts, use allowed alternate senders, and attach files |
| Organization | Create and manage folders, apply Proton labels, change flags, archive, move, copy, restore, and manage Spam or Trash |
| Bounded bulk actions | Preview, mark, star, move, copy, archive, trash, restore, or permanently delete explicit UID lists |
| Automations | Poll persistent cursors or push new-message and new-alias events to a webhook, JSONL file, or command |
| SimpleLogin | Inspect account data and manage aliases, contacts, and mailboxes with an optional API key |
| Deployment | Local stdio, localhost Streamable HTTP, or OAuth/OIDC-protected hosted HTTP |

Proton Contacts are not included because Proton Mail Bridge does not expose them.

## Automations

The background watcher polls for new mail or newly created SimpleLogin aliases and emits one event per new item:

```bash
proton-workflow-watch \
  --env-file ~/.config/proton-workflow-connector/env \
  --folder INBOX \
  --webhook-url https://example.com/hooks/proton \
  --interval 60
```

Key behavior:

- The first run establishes a baseline and does not replay the existing mailbox.
- Persistent cursors survive restarts and account for IMAP `UIDVALIDITY` changes.
- Webhook delivery is at-least-once, with retries, exponential backoff, and dead-letter handling.
- An optional HMAC-SHA256 signature lets webhook receivers verify each event.
- Events contain message metadata only; bodies and attachments remain available on demand through MCP tools.
- JSON rules can run several independently filtered triggers from one watcher.
- `--dry-run` shows which events and rule actions would fire without delivering, acting, or advancing cursors.

Read [Triggers and webhooks](docs/WATCH.md) for event schemas, filters, delivery guarantees, signature verification, rules, and systemd examples.

## Security model

This connector processes private mail. Its defaults and boundaries are intentionally explicit:

- **Prefer local stdio.** Bridge and the connector should stay on the same trusted machine whenever possible.
- **Keep credentials private.** Store Bridge-generated credentials and optional API keys outside Git with user-only permissions.
- **Confirm destructive actions.** Permanent deletion, empty-folder operations, folder deletion, and alias deletion require `confirm=true` after explicit user intent.
- **Preview before mutating.** Sends, forwards, replies, bulk actions, permanent delete, and empty-folder operations support `dry_run=true` previews.
- **Treat mail as untrusted.** Read/search results mark email content as `content_trust: "untrusted"`, and outbound HTML is sanitized unless `trusted_html=true`.
- **Bound bulk operations.** Bulk tools require explicit numeric UIDs and default to a maximum of 50 messages per call.
- **Restrict senders.** Mail can only be sent from `PROTON_BRIDGE_EMAIL` or an address in `PROTON_BRIDGE_SENDER_ADDRESSES`.
- **Protect remote access.** Non-local HTTP deployments require deliberate Host/Origin policy; internet deployments require HTTPS and an external OAuth/OIDC provider.
- **Limit exposed data.** Optional audit records exclude addresses, message bodies, attachments, credentials, and tokens.
- **Treat events as untrusted input.** Email subjects and alias names can be attacker-controlled; webhook receivers and commands must validate event fields.

Read [SECURITY.md](SECURITY.md) before using real mail and [Hosted HTTP setup](docs/HOSTING.md) before exposing the connector beyond localhost.

## Known boundaries

- Proton Mail Bridge must remain running while the connector or watcher is active.
- One server process connects to one Bridge account.
- Proton Contacts, native scheduled sending, filters, account settings, and key management are not exposed by Bridge and are not emulated.
- New-mail and new-alias triggers use polling because Proton and SimpleLogin do not provide a compatible push API.
- Trigger events include metadata, not message bodies or attachments.
- `All Mail` is a virtual read-only Bridge mailbox; start move and permanent-delete operations from a writable folder.

## Documentation

| Guide | Use it for |
| --- | --- |
| [Setup](docs/SETUP.md) | Bridge configuration, private environment files, folder names, and network modes |
| [Tool reference](docs/TOOLS.md) | All 67 MCP tools, arguments, safety limits, and boundaries |
| [Client setup](docs/CLIENTS.md) | Generic MCP clients, local agents, Codex, Claude Code, plugins, and Streamable HTTP |
| [Triggers and webhooks](docs/WATCH.md) | Watcher rules, payloads, delivery behavior, and signature verification |
| [Hosted HTTP](docs/HOSTING.md) | OAuth/OIDC, HTTPS, Host/Origin validation, scopes, audit logs, and systemd |
| [Integration tests](docs/INTEGRATION_TESTS.md) | Live Bridge and SimpleLogin verification |

Official upstream references:

- [Proton Mail Bridge](https://proton.me/mail/bridge)
- [Proton IMAP/SMTP setup](https://proton.me/support/imap-smtp-and-pop3-setup)
- [SimpleLogin API](https://github.com/simple-login/app/blob/master/docs/api.md)

## Development

Install the development dependencies and run the local checks:

```bash
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
python -m compileall -q src
pip-audit
gitleaks detect --redact --config .gitleaks.toml
```

Tests use synthetic data and do not require access to a real mailbox. Live checks are documented separately in [Integration tests](docs/INTEGRATION_TESTS.md).

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution and privacy requirements. Report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).

<!-- mcp-name: io.github.ntrpydev/proton-workflow-connector -->

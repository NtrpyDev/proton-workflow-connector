# Proton Workflow Connector

This is an unofficial, self-hosted MCP connector for Proton Mail Bridge and SimpleLogin.

This project is not affiliated with, endorsed by, or sponsored by Proton AG. Each user runs Proton Mail Bridge locally and supplies their own Bridge credentials and optional SimpleLogin API key.

## What v1 does

- Read and manage Proton Mail through Bridge IMAP.
- Send, reply, reply-all, and forward through Bridge SMTP.
- Download attachments and attach files to mail and drafts.
- Search one folder or every selectable folder at once.
- Manage folders, drafts, flags, Spam, Trash, and bounded bulk operations.
- Use any sending address explicitly allowed in the private configuration.
- Manage SimpleLogin aliases, contacts, and mailboxes with an optional API key.
- Connect over local stdio, private HTTP, or OAuth-protected hosted HTTP.

The server exposes 58 tools. [docs/TOOLS.md](docs/TOOLS.md) describes each one. Proton Contacts are not included because Bridge does not expose them.

## Installation

```bash
git clone https://github.com/NtrpyDev/proton-workflow-connector.git
cd proton-workflow-connector
python -m pip install -e ".[dev]"
```

Copy `.env.example` to a private file outside version control and fill it with credentials from Proton Mail Bridge:

```bash
cp .env.example ~/.config/proton-workflow-connector/env
chmod 600 ~/.config/proton-workflow-connector/env
```

Run the server locally:

```bash
proton-workflow-connector --transport stdio --env-file ~/.config/proton-workflow-connector/env
```

## Proton Mail Bridge setup

Install Proton Mail Bridge from Proton, sign in, and use the Bridge app to find the local IMAP/SMTP host, ports, username, and generated Bridge password. The example environment file uses common local defaults, but users should confirm their own values in Bridge.

Official references:

- Proton Mail Bridge: https://proton.me/mail/bridge
- IMAP/SMTP setup: https://proton.me/support/imap-smtp-and-pop3-setup
- Linux setup: https://proton.me/support/bridge-for-linux

## SimpleLogin setup

SimpleLogin tools are optional. Create or copy an API code from SimpleLogin and set `SIMPLELOGIN_API_KEY` in your private environment file. The API uses the `Authentication` header as documented upstream.

Reference: https://github.com/simple-login/app/blob/master/docs/api.md

## Client setup

See [docs/CLIENTS.md](docs/CLIENTS.md) for client examples. See [docs/HOSTING.md](docs/HOSTING.md) before making an HTTP endpoint reachable from another machine.

For Claude Code over stdio:

```bash
claude mcp add --transport stdio --scope user proton-workflow \
  -- proton-workflow-connector --transport stdio --env-file ~/.config/proton-workflow-connector/env
```

For Codex, add this server entry to `config.toml`:

```toml
[mcp_servers.proton_workflow]
command = "proton-workflow-connector"
args = ["--transport", "stdio"]
env_vars = [
  "PROTON_BRIDGE_IMAP_HOST",
  "PROTON_BRIDGE_IMAP_PORT",
  "PROTON_BRIDGE_IMAP_TLS",
  "PROTON_BRIDGE_SMTP_HOST",
  "PROTON_BRIDGE_SMTP_PORT",
  "PROTON_BRIDGE_SMTP_TLS",
  "PROTON_BRIDGE_ALLOW_INSECURE_TLS",
  "PROTON_BRIDGE_USERNAME",
  "PROTON_BRIDGE_PASSWORD",
  "PROTON_BRIDGE_EMAIL",
  "PROTON_BRIDGE_SENDER_ADDRESSES",
  "SIMPLELOGIN_API_KEY"
]
default_tools_approval_mode = "prompt"
startup_timeout_sec = 20
tool_timeout_sec = 120
```

## Keeping private data out of Git

Do not commit real credentials, Bridge passwords, SimpleLogin API keys, mailbox exports, message bodies, screenshots, logs, private hostnames, or private IPs. Keep local setup notes outside this repo or in ignored private files.

The repository has a few safeguards for this:

- `.env.example` with fake placeholder values only.
- `.gitignore` rules for local config, logs, caches, mailbox exports, and private notes.
- Local pre-commit hooks for Ruff and Gitleaks.
- CI for lint/tests and PR secret scanning.
- A release checklist in [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).

## Development checks

```bash
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
python -m compileall -q src
pip-audit
```

If Gitleaks is installed, scan for secrets too:

```bash
gitleaks detect --redact --config .gitleaks.toml
```

## License

MIT. See [LICENSE](LICENSE).

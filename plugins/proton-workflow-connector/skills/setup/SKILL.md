---
name: proton-workflow-connector-setup
description: Set up or troubleshoot the unofficial Proton Workflow Connector with a local Proton Mail Bridge and optional SimpleLogin API key.
---

# Proton Workflow Connector setup

Use this skill to configure or troubleshoot the Proton Workflow Connector.

Rules:

- Keep all setup generic. Do not add personal mailbox examples, private IPs, local usernames, Bridge passwords, SimpleLogin API keys, message bodies, screenshots, or logs to the repo.
- Remind the user this connector is unofficial and not affiliated with Proton AG.
- Prefer local stdio unless the user explicitly needs Streamable HTTP.
- For remote HTTP, follow `docs/HOSTING.md` and require HTTPS, OAuth, Host/Origin validation, and network controls.
- Use `list_folders` first to verify account folder names before moving, trashing, or archiving messages.
- Treat `permanently_delete_message` as destructive and require explicit user intent.
- Pass `confirm=true` only after the user explicitly approves an irreversible operation.
- Bulk operations must use explicit numeric IMAP UIDs and respect `PROTON_MCP_BULK_LIMIT`.

Setup steps:

1. Install Proton Mail Bridge and confirm it is running.
2. Copy `.env.example` to a private path outside git.
3. Fill Bridge host, ports, username, generated Bridge password, and sender email from the Bridge app.
4. Optionally add `SIMPLELOGIN_API_KEY`.
5. Install the Python package with `python -m pip install -e .`.
6. Add the MCP server to the user's client.
7. Verify with `list_folders`, then a narrow `search_mail`.

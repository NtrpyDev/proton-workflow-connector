# Security

This connector processes private mail through the user's own Proton Mail Bridge instance. Treat every environment file, log, screenshot, and test artifact as sensitive.

## Reporting a vulnerability

Once the public repository is available, report vulnerabilities through a private GitHub security advisory. Do not open a public issue for a security report.

Do not include real message bodies, credentials, API keys, or mailbox exports in public issues.

## Running the server safely

- Keep Proton Mail Bridge bound to localhost where possible.
- Never send Bridge credentials over a remote plaintext or certificate-unverified IMAP/SMTP
  connection. The connector rejects those combinations for non-loopback Bridge hosts.
- Prefer local stdio MCP connections.
- Follow `docs/HOSTING.md` before exposing Streamable HTTP beyond localhost.
- Use OAuth/OIDC, HTTPS, Host validation, Origin validation, and rate limits for internet deployments.
- Keep `.env` files outside git and chmod them to user-only access.
- Use `permanently_delete_message` only after explicit user confirmation.

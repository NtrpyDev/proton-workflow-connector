# Release checklist

Check each item before publishing a release:

- Run `ruff check .`.
- Run `ruff format --check .`.
- Run `pytest`.
- Run `python -m compileall -q src`.
- Run `pip-audit`.
- Build and install the wheel in a clean environment.
- Run `gitleaks detect --redact --config .gitleaks.toml`.
- Confirm `.env.example` contains fake placeholder values only.
- Confirm no real Bridge credentials, Bridge passwords, SimpleLogin API keys, mailbox exports, message bodies, screenshots, logs, or private deployment files are present.
- Confirm no private IP addresses, private hostnames, usernames, or account-specific setup values are present.
- Confirm there are no personal names except intentionally public author metadata.
- Confirm no real domains or mailboxes are present unless intentionally public.
- Confirm test fixtures use reserved example addresses such as `alice@example.com`.
- Confirm git history does not contain API keys or credentials.
- Confirm release notes include the unofficial Proton disclaimer.
- Confirm all destructive tools reject calls without `confirm=true`.
- Confirm hosted HTTP rejects unauthenticated public access.
- Confirm the version matches in `pyproject.toml`, the package, and the Codex plugin manifest.

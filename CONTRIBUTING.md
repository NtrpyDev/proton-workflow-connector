# Contributing

Do not put account-specific or private data in this repository.

Before opening a pull request, run:

```bash
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
python -m compileall -q src
pip-audit
gitleaks detect --redact --config .gitleaks.toml
```

Tests must use fake data. Use addresses under `example.com`, short synthetic message bodies, and made-up IDs.

Do not add account-specific screenshots, mailbox exports, logs, private IPs, private hostnames, usernames, credentials, or local deployment notes.

Later: if other people start maintaining releases, add a short maintainer checklist.

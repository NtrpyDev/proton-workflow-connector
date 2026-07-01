# Streamable HTTP example

Start the server on localhost:

```bash
proton-workflow-connector --transport streamable-http --host 127.0.0.1 --port 8765 \
  --env-file ~/.config/proton-workflow-connector/env
```

Use this client URL:

```text
http://127.0.0.1:8765/mcp
```

Do not expose this endpoint beyond localhost without adding HTTPS, authentication, and network controls.

For LAN or internet access, follow `docs/HOSTING.md`.

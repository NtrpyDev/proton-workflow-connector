# MCP client setup

Proton Workflow Connector (PWC) is self-hosted. Use local stdio when the client and connector run on the same machine.
Use Streamable HTTP when the client needs a URL.

Any MCP client or local agent runtime can use PWC if it can launch an MCP stdio server or call a Streamable HTTP MCP endpoint.

## Generic MCP clients and local agents

For stdio clients, configure this server command:

```bash
proton-workflow-connector --transport stdio \
  --env-file ~/.config/proton-workflow-connector/env
```

For HTTP clients, start the server on localhost:

```bash
proton-workflow-connector --transport streamable-http --host 127.0.0.1 --port 8765 \
  --env-file ~/.config/proton-workflow-connector/env
```

Then point the client at:

```text
http://127.0.0.1:8765/mcp
```

Keep HTTP on localhost unless you have completed [HOSTING.md](HOSTING.md).

## Claude Code

Claude Code supports local stdio MCP servers and remote HTTP MCP servers.

Install as a plugin (easiest):

```
/plugin marketplace add NtrpyDev/proton-workflow-connector
/plugin install proton-workflow-connector@proton-workflow-connector
```

The plugin runs `proton-workflow-connector` from your PATH, so install the package first
(`pip install proton-workflow-connector`) and put your Bridge credentials in the environment
Claude Code starts from.

Or add the local stdio server directly:

```bash
claude mcp add --transport stdio --scope user proton-workflow \
  -- proton-workflow-connector --transport stdio --env-file ~/.config/proton-workflow-connector/env
```

Use `/mcp` in Claude Code to inspect the connection.

For `claude.ai/code` Remote Control, verify the local connection with `/mcp` first.
Then start the Remote Control session and try a read-only request such as listing folders.
Bridge must stay running on the same machine as the MCP server.

For an HTTP connection:

```bash
proton-workflow-connector --transport streamable-http --host 127.0.0.1 --port 8765 \
  --env-file ~/.config/proton-workflow-connector/env

claude mcp add --transport http proton-workflow http://127.0.0.1:8765/mcp
```

For hosted custom connectors, configure OAuth and HTTPS as described in [HOSTING.md](HOSTING.md).

References:

- Claude Code MCP: https://docs.anthropic.com/en/docs/claude-code/mcp
- Claude custom connectors: https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp

## Codex configuration

Add a local stdio server to `~/.codex/config.toml` or a trusted project-scoped `.codex/config.toml`:

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
  "SIMPLELOGIN_API_KEY",
  "SIMPLELOGIN_BASE_URL"
]
default_tools_approval_mode = "prompt"
startup_timeout_sec = 20
tool_timeout_sec = 120
```

You can also point a user-level Codex configuration at a private environment file:

```toml
[mcp_servers.proton_workflow]
command = "proton-workflow-connector"
args = ["--transport", "stdio", "--env-file", "/path/to/private/proton-workflow-connector-env"]
default_tools_approval_mode = "prompt"
```

Reference: https://developers.openai.com/codex/codex-manual.md

## Codex plugin wrapper

The repo includes a v1 plugin wrapper at `plugins/proton-workflow-connector`.

The wrapper contains no credentials or personal marketplace configuration. Install the Python package separately, then pass Bridge and SimpleLogin settings through environment variables.

For local testing, copy the plugin directory into a Codex marketplace source or include it in a repo marketplace. Keep any private marketplace file or private env file out of public git history.

## Other HTTP clients

Run the server:

```bash
proton-workflow-connector --transport streamable-http --host 127.0.0.1 --port 8765 \
  --env-file ~/.config/proton-workflow-connector/env
```

Point the MCP client at:

```text
http://127.0.0.1:8765/mcp
```

Use localhost unless you have completed the hosted setup in [HOSTING.md](HOSTING.md).

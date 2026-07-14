# Hosted HTTP setup

Use this guide when the MCP endpoint must be reachable from another machine.
One running server connects to one Proton Bridge account.

## Localhost

Localhost HTTP does not require OAuth:

```bash
proton-workflow-connector --transport streamable-http --host 127.0.0.1 --port 8765 \
  --env-file ~/.config/proton-workflow-connector/env
```

## Private LAN

Non-local HTTP requires an allowed Host value.
OAuth remains the recommended choice.
A trusted private network can opt out explicitly:

```dotenv
PROTON_MCP_HTTP_ALLOWED_HOSTS=192.0.2.10:8765
PROTON_MCP_ALLOW_UNAUTHENTICATED_HTTP=true
```

The documentation address above is an example. Use the server's actual private address only in the private environment file.

## Internet hosting

Internet deployments require an external OAuth/OIDC provider that issues signed JWT access tokens. Configure:

```dotenv
PROTON_MCP_OAUTH_ISSUER_URL=https://login.example.com
PROTON_MCP_OAUTH_AUDIENCE=https://mail.example.com/mcp
PROTON_MCP_OAUTH_RESOURCE_SERVER_URL=https://mail.example.com/mcp
PROTON_MCP_OAUTH_JWKS_URL=
PROTON_MCP_OAUTH_BASE_SCOPE=proton-workflow-connector
PROTON_MCP_HTTP_ALLOWED_HOSTS=mail.example.com
PROTON_MCP_HTTP_ALLOWED_ORIGINS=https://client.example.com
PROTON_MCP_AUDIT_LOG=/var/log/proton-workflow-connector/audit.jsonl
```

`PROTON_MCP_OAUTH_JWKS_URL` is optional when the provider publishes standard OIDC discovery metadata.

Tokens need the base `proton-workflow-connector` scope plus the permissions used by the client:

- `mail.read`
- `mail.write`
- `mail.delete`
- `simplelogin.read`
- `simplelogin.write`
- `simplelogin.delete`

`proton-workflow-connector.admin` satisfies all operation-level checks.

## HTTPS proxy

Run the MCP process on localhost and terminate HTTPS in a reverse proxy. A minimal Caddy configuration is:

```caddyfile
mail.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Start the MCP process with the hosted environment file:

```bash
proton-workflow-connector --transport streamable-http --host 127.0.0.1 --port 8765 \
  --env-file /path/to/private/hosted.env
```

The server rejects a public HTTP resource URL, invalid Host or Origin headers, missing OAuth tokens, expired tokens, invalid signatures, wrong issuers, wrong audiences, and missing permissions.

## User services on Linux

The repository includes example user-level systemd units in `examples/systemd`. Review their paths and network settings before installing them:

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now protonmail-bridge-headless.service proton-workflow-connector.service
```

The MCP example binds to localhost for use behind an HTTPS proxy.
Change the environment filename if your private file is not `~/.config/proton-workflow-connector/env`.

The Bridge unit includes an explicit `flatpak kill` stop action.
Flatpak runs Bridge in a separate application scope, so `systemctl --user restart protonmail-bridge-headless.service` needs that line to replace the actual Bridge process.

If Proton Mail Bridge is installed as a native package that already provides
`protonmail-bridge.service`, replace the connector's Flatpak dependency with the
included drop-in:

```bash
mkdir -p ~/.config/systemd/user/proton-workflow-connector.service.d
cp examples/systemd/proton-workflow-connector-native-bridge.conf \
  ~/.config/systemd/user/proton-workflow-connector.service.d/bridge.conf
systemctl --user daemon-reload
systemctl --user enable --now protonmail-bridge.service proton-workflow-connector.service
```

Do not enable both Bridge units. Confirm the native unit is authenticated and
fully synchronized before relying on connector automation.

## Secrets and logs

- Put Bridge credentials and OAuth configuration in a user-only environment file or server secret store.
- Do not put Proton login credentials in this connector. It uses Bridge-generated credentials.
- Audit records include operation names, account-independent IDs, outcomes, and OAuth subjects. They exclude addresses, message bodies, attachment data, credentials, and tokens.
- Rate limits apply per authenticated subject for read, write, and destructive operations.

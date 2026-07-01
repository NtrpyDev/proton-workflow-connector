# Setup

## 1. Install Proton Mail Bridge

Install Proton Mail Bridge, sign in, and keep Bridge running while the MCP server is running.

Open the Bridge app and copy these connection details:

- IMAP host and port
- SMTP host and port
- Bridge username
- Bridge-generated password
- Security mode for IMAP and SMTP

Official references:

- https://proton.me/mail/bridge
- https://proton.me/support/imap-smtp-and-pop3-setup
- https://proton.me/support/bridge-for-linux

## 2. Create the environment file

Create a private env file:

```bash
mkdir -p ~/.config/proton-workflow-connector
cp .env.example ~/.config/proton-workflow-connector/env
chmod 600 ~/.config/proton-workflow-connector/env
```

Replace the example values with the details from Bridge. Keep this file out of Git.

Set `PROTON_BRIDGE_EMAIL` to the default sender. If the account has additional Proton or custom-domain addresses, list every allowed sender in `PROTON_BRIDGE_SENDER_ADDRESSES`, separated by commas.

## 3. Install and run the server

```bash
python -m pip install -e .
proton-workflow-connector --transport stdio --env-file ~/.config/proton-workflow-connector/env
```

To use Streamable HTTP instead:

```bash
proton-workflow-connector --transport streamable-http --host 127.0.0.1 --port 8765 \
  --env-file ~/.config/proton-workflow-connector/env
```

## 4. Check folder names

Run the `list_folders` MCP tool first. Folder names vary by account and Bridge version. If the returned names differ from the defaults, update these variables:

```text
PROTON_ARCHIVE_FOLDER
PROTON_TRASH_FOLDER
PROTON_DRAFTS_FOLDER
PROTON_SENT_FOLDER
PROTON_SPAM_FOLDER
```

## 5. Add SimpleLogin if needed

Set `SIMPLELOGIN_API_KEY` only if you want the SimpleLogin tools. Leave it unset to use Proton Mail tools only.

## 6. Choose the network mode

- Use stdio when the MCP client and server run on the same machine.
- Use localhost HTTP when a local client requires HTTP.
- Read [HOSTING.md](HOSTING.md) before binding HTTP to a LAN address or public hostname.

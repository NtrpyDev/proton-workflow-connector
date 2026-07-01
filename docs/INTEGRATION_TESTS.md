# Integration tests

These checks use live local services, so CI does not run them.

## Proton Mail Bridge

1. Start Proton Mail Bridge.
2. Configure a private env file with Bridge credentials.
3. Connect with an MCP client.
4. Run:

- `list_folders`
- `search_mail` with a narrow query
- `read_mail` on one known test UID
- `create_draft`, then `delete_draft`
- `send_mail` to a mailbox you control
- `move_message` or `trash_message` on a disposable test message
- `search_all_mail` with a unique synthetic subject
- `reply_mail`, `reply_all`, and `forward_mail` on synthetic messages
- send and download a small synthetic attachment, then compare its bytes
- create two drafts, permanently delete one UID, and verify the other remains
- create, rename, subscribe, unsubscribe, and delete a temporary folder

Do not commit logs, message bodies, screenshots, mailbox exports, or Bridge notes from this process.

### Automated live workflow

The live workflow creates folders and drafts, sends test messages, exercises message actions, and permanently removes data carrying its unique test marker. Use a recipient mailbox you control. The recipient must not be one of the configured sender addresses if `reply_all` is expected to have a recipient.

```bash
python scripts/live_bridge_workflow.py \
  --url http://127.0.0.1:8765/mcp \
  --sender sender@example.com \
  --recipient controlled-test-mailbox@example.com
```

Do not run this script where automatic test-data cleanup is unacceptable. It never empties a folder, but it permanently deletes messages containing its generated `MCP-v1-live-...` marker.

## SimpleLogin

Use a disposable alias and run this sequence:

1. `simplelogin_user_info`
2. `simplelogin_list_mailboxes`
3. `simplelogin_create_random_alias`
4. `simplelogin_toggle_alias`
5. `simplelogin_delete_alias`

## Client checks

- Claude Code: add the local stdio server and verify `/mcp`.
- Codex direct MCP: add the config and verify the tools list.
- Codex plugin: install from the plugin wrapper and confirm the MCP server is visible.

## Hosted HTTP

Use a test OIDC issuer and HTTPS hostname. Verify valid access, then verify rejection of:

- missing, expired, and incorrectly signed tokens
- wrong issuer or audience
- missing operation scopes
- invalid Host and Origin headers
- public HTTP resource URLs
- requests over configured rate limits

# Tool reference

This connector exposes 60 MCP tools: 46 for Proton Mail through Bridge, 13 for SimpleLogin, and one status tool.

## Folders

- `list_folders`: List folders and IMAP flags.
- `folder_status`: Return total, unread, UIDNEXT, and UIDVALIDITY values.
- `create_folder`: Create a folder or label using its full Bridge mailbox name.
- `rename_folder`: Rename a folder or label.
- `delete_folder`: Permanently delete a folder after `confirm=true`.
- `subscribe_folder`: Subscribe to a folder.
- `unsubscribe_folder`: Unsubscribe from a folder.

## Search and reading

- `search_mail`: Search one folder by text, addresses, subject, date, read status, or star.
- `search_all_mail`: Search every selectable folder and remove duplicate Message-IDs.
- `read_mail`: Read one UID with an optional body limit and read-state update.
- `read_thread`: Find messages linked by `Message-ID`, `References`, and `In-Reply-To`.
- `inspect_attachments`: List attachment metadata, including inline parts.
- `download_attachment`: Return one attachment as Base64 with its MIME metadata.

## Triggers

- `poll_mailbox`: Return messages that arrived since the last call, using a persistent per-cursor UID position. The first call for a cursor baselines to the current mailbox head and returns nothing, so you only ever receive genuinely new mail. Pass a stable `cursor_name` to track several independent triggers over one folder. This is the tool an agent uses to build "when new mail matching X arrives, do Y" loops.
- `poll_aliases`: The SimpleLogin counterpart of `poll_mailbox`. Return aliases created since the last call, using a persistent cursor on the highest alias id. The first call baselines and returns nothing; later calls return only new aliases. `query` matches a substring of the alias email. Requires `SIMPLELOGIN_API_KEY`.

For background push delivery to a webhook, file, or command, see [WATCH.md](WATCH.md).

## Sending and drafts

- `send_mail`: Send text or HTML with CC, BCC, Reply-To, alternate senders, and attachments.
- `reply_mail`: Reply to the sender with correct thread headers.
- `reply_all`: Reply to all while excluding configured sender addresses.
- `forward_mail`: Forward text, HTML, and optional original attachments.
- `create_draft`: Create a draft with an alternate sender and attachments.
- `update_draft`: Create the replacement before deleting the old draft.
- `delete_draft`: Move a draft through Trash and expunge it after `confirm=true`.
- `send_draft`: Send a saved draft, then remove it after SMTP succeeds.

Attachment inputs use `filename`, `content_type`, and `content_base64`. Optional fields are `disposition` (`attachment` or `inline`) and `content_id`. The defaults follow Proton's 25 MB outgoing, 50 MB incoming, and 100-file limits.

## Message management

- `mark_read` and `mark_unread`: Change the IMAP Seen flag.
- `star_message` and `unstar_message`: Change the IMAP Flagged flag.
- `move_message` and `copy_message`: Move or copy one UID.
- `archive_message` and `trash_message`: Move one UID to the configured folder.
- `mark_spam` and `mark_not_spam`: Move mail into or out of Spam.
- `restore_message`: Move one UID from Trash or another folder.
- `permanently_delete_message`: Move one UID from a writable folder through Trash, then selectively expunge it after `confirm=true`.

## Bulk and empty-folder operations

- `bulk_mark_read` and `bulk_mark_unread`
- `bulk_star` and `bulk_unstar`
- `bulk_move` and `bulk_copy`
- `bulk_archive`, `bulk_trash`, and `bulk_restore`
- `bulk_permanently_delete`: Move explicit UIDs through Trash and selectively expunge them after `confirm=true`.
- `empty_trash`: Selectively expunge every UID in the configured Trash folder after `confirm=true`.
- `empty_spam`: Move every Spam UID through Trash and expunge it after `confirm=true`.

`All Mail` is a virtual read-only mailbox in Proton Bridge. Search and read it normally, but start move or permanent-delete operations from a writable folder such as Inbox, Sent, Archive, Spam, Trash, or a user folder. Bridge synchronization can briefly leave a deleted message visible in `All Mail` after the Trash expunge succeeds.

Bulk tools require explicit numeric UIDs and are capped by `PROTON_MCP_BULK_LIMIT`, which defaults to 50.

## SimpleLogin

- `simplelogin_user_info` and `simplelogin_stats`
- `simplelogin_list_aliases` and `simplelogin_get_alias`
- `simplelogin_create_random_alias` and `simplelogin_create_custom_alias`
- `simplelogin_update_alias` and `simplelogin_toggle_alias`
- `simplelogin_delete_alias`: Delete an alias after `confirm=true`.
- `simplelogin_list_alias_contacts` and `simplelogin_create_alias_contact`
- `simplelogin_list_mailboxes`
- `poll_aliases`: New-alias trigger, described under [Triggers](#triggers) above.

SimpleLogin tools require `SIMPLELOGIN_API_KEY`. The rest of the server works without it.

## Status

- `server_status`: Check IMAP, SMTP, SimpleLogin, OAuth configuration, and server version without returning secrets.

## Boundaries

- One server process connects to one Proton Bridge account.
- Only addresses in `PROTON_BRIDGE_EMAIL` or `PROTON_BRIDGE_SENDER_ADDRESSES` may be used as senders.
- Proton Contacts, native scheduled sending, filters, account settings, and key management are not available through Bridge and are not emulated.
- Hosted HTTP requires an external OAuth/OIDC provider and HTTPS. The server does not store Proton login sessions.

#!/usr/bin/env python3
"""Live acceptance harness for PWC.

Runs marker-scoped suites against a live server and produces a redacted JSON report plus a
coverage matrix over every public MCP tool. Destructive operations touch only data carrying
this run's unique marker; empty_trash/empty_spam abort when non-marker mail is present.

Suites:
  env-gate     capability checklist (always runs first)
  bridge       full mail tool coverage over live Bridge
  simplelogin  alias lifecycle with marker-tagged aliases
  watch        watcher sinks, HMAC webhooks, dead-letter, replay (spawns proton-workflow-watch;
               run on the Bridge host with --env-file and the fixture Worker flags)
  safety       read-only / allow-send / allowed-actions / rate-limit / audit modes (spawns its
               own server instances; run on the Bridge host with --env-file)
  oauth        hosted token matrix (point --url at the public HTTPS endpoint and provide the
               fixture Worker flags)
  all          bridge + simplelogin, plus watch/safety/oauth when their flags are present

Checks end in one of three states: pass, fail, or environment-incomplete. A missing provider
capability is reported as environment-incomplete, never as a pass.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

PASS = "pass"
FAIL = "fail"
INCOMPLETE = "environment-incomplete"

# Tools that only the later suites can exercise. `all` still counts them as required coverage;
# suite-scoped runs list them as out-of-scope instead of failing the matrix.
SUITE_TOOLS = {
    "bridge": {
        "server_status",
        "list_folders",
        "folder_status",
        "list_labels",
        "search_mail",
        "search_all_mail",
        "read_mail",
        "read_thread",
        "get_headers",
        "inspect_attachments",
        "download_attachment",
        "create_draft",
        "update_draft",
        "delete_draft",
        "send_draft",
        "send_mail",
        "reply_mail",
        "reply_all",
        "forward_mail",
        "draft_reply",
        "draft_forward",
        "mark_read",
        "mark_unread",
        "star_message",
        "unstar_message",
        "apply_label",
        "remove_label",
        "move_message",
        "copy_message",
        "archive_message",
        "trash_message",
        "restore_message",
        "mark_spam",
        "mark_not_spam",
        "create_folder",
        "rename_folder",
        "subscribe_folder",
        "unsubscribe_folder",
        "delete_folder",
        "bulk_mark_read",
        "bulk_mark_unread",
        "bulk_star",
        "bulk_unstar",
        "bulk_move",
        "bulk_copy",
        "bulk_archive",
        "bulk_trash",
        "bulk_restore",
        "bulk_permanently_delete",
        "permanently_delete_message",
        "empty_trash",
        "empty_spam",
        "unsubscribe",
        "poll_mailbox",
    },
    "simplelogin": {
        "simplelogin_user_info",
        "simplelogin_stats",
        "simplelogin_list_mailboxes",
        "simplelogin_list_aliases",
        "simplelogin_get_alias",
        "simplelogin_create_random_alias",
        "simplelogin_create_custom_alias",
        "simplelogin_update_alias",
        "simplelogin_toggle_alias",
        "simplelogin_delete_alias",
        "simplelogin_list_alias_contacts",
        "simplelogin_create_alias_contact",
        "poll_aliases",
    },
}


class CheckFailure(RuntimeError):
    pass


class EnvironmentIncomplete(RuntimeError):
    pass


class Harness:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.marker = f"PWC-live-{int(time.time())}"
        self.folder = f"Folders/{self.marker}"
        self.label = f"{self.marker}-label"
        self.attachment = f"attachment {self.marker}\n".encode()
        self.checks: list[dict[str, Any]] = []
        self.tools_called: set[str] = set()
        self.tools_available: list[str] = []
        self.session: ClientSession | None = None

    # ------------------------------------------------------------------ plumbing

    async def raw_call(self, name: str, arguments: dict[str, Any] | None = None):
        self.tools_called.add(name)
        assert self.session is not None
        return await asyncio.wait_for(self.session.call_tool(name, arguments or {}), timeout=self.args.timeout)

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = await self.raw_call(name, arguments)
        if result.isError:
            detail = " ".join(getattr(item, "text", "") for item in result.content)
            raise CheckFailure(f"{name}: {detail}")
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured.get("result", structured) if isinstance(structured, dict) else structured
        for item in result.content:
            if getattr(item, "type", None) == "text":
                return json.loads(item.text)
        raise CheckFailure(f"{name} returned no decodable content")

    async def expect_error(self, name: str, arguments: dict[str, Any], why: str) -> None:
        result = await self.raw_call(name, arguments)
        if not result.isError:
            raise CheckFailure(f"{name} unexpectedly succeeded: {why}")

    async def check(self, name: str, coro) -> bool:
        started = time.monotonic()
        try:
            await coro
        except EnvironmentIncomplete as exc:
            self._record(name, INCOMPLETE, str(exc), started)
            return False
        except Exception as exc:
            self._record(name, FAIL, _describe(exc), started)
            return False
        self._record(name, PASS, "", started)
        return True

    def _record(self, name: str, status: str, detail: str, started: float) -> None:
        entry = {
            "check": name,
            "status": status,
            "seconds": round(time.monotonic() - started, 2),
        }
        if detail:
            entry["detail"] = _redact(detail)
        self.checks.append(entry)
        print(f"{status}: {name}" + (f" — {entry.get('detail')}" if detail else ""), flush=True)

    async def search(self, folder: str, *, attempts: int = 1, query: str | None = None) -> list[dict[str, Any]]:
        for _ in range(attempts):
            rows = await self.call("search_mail", {"folder": folder, "query": query or self.marker, "limit": 100})
            if rows:
                return rows
            await asyncio.sleep(2)
        return []

    def with_sender(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.args.sender:
            arguments["from_address"] = self.args.sender
        return arguments

    def to_both(self) -> list[str]:
        """Outside recipient plus the sender itself, so a copy lands in INBOX for later checks."""
        if self.args.sender:
            return [self.args.recipient, self.args.sender]
        return [self.args.recipient]

    # ------------------------------------------------------------------ env gate

    async def suite_env_gate(self) -> None:
        await self.check("env: server_status services up", self._gate_status())
        await self.check("env: tool inventory", self._gate_tools())
        await self.check("env: sender identity accepted", self._gate_sender())
        await self.check("env: recipient configured", self._gate_recipient())

    async def _gate_status(self) -> None:
        status = await self.call("server_status")
        bridge = status.get("bridge", {})
        if not (bridge.get("imap") and bridge.get("smtp")):
            raise EnvironmentIncomplete("Bridge IMAP/SMTP not reachable from the server")
        simplelogin = status.get("simplelogin", {})
        if not simplelogin.get("configured"):
            raise EnvironmentIncomplete("SimpleLogin is not configured")
        if not simplelogin.get("reachable"):
            raise EnvironmentIncomplete("SimpleLogin API not reachable")

    async def _gate_tools(self) -> None:
        assert self.session is not None
        listing = await self.session.list_tools()
        self.tools_available = sorted(tool.name for tool in listing.tools)
        known = SUITE_TOOLS["bridge"] | SUITE_TOOLS["simplelogin"]
        unknown = [name for name in self.tools_available if name not in known]
        if unknown:
            raise CheckFailure(
                "tools outside the coverage map (extend SUITE_TOOLS or a later suite): " + ", ".join(unknown)
            )

    async def _gate_sender(self) -> None:
        if not self.args.sender:
            raise EnvironmentIncomplete("--sender not provided")
        preview = await self.call(
            "send_mail",
            self.with_sender(
                {
                    "to": self.args.recipient,
                    "subject": f"{self.marker} env gate",
                    "text": "env gate",
                    "dry_run": True,
                }
            ),
        )
        if not preview.get("dry_run"):
            raise CheckFailure("dry_run send did not return a dry_run preview")
        if self.args.sender not in str(preview.get("sender", "")):
            raise CheckFailure("server did not accept the configured sender identity")

    async def _gate_recipient(self) -> None:
        if not self.args.recipient:
            raise EnvironmentIncomplete("--recipient not provided")
        if self.args.sender and self.args.recipient.lower() == self.args.sender.lower():
            raise CheckFailure("recipient must differ from sender for reply/reply_all checks")

    # ------------------------------------------------------------------ bridge suite

    async def suite_bridge(self) -> None:
        await self.check("bridge: folder lifecycle", self._folder_lifecycle())
        await self.check("bridge: dry-run send is side-effect free", self._dry_run_send())
        await self.check("bridge: draft lifecycle and attachments", self._draft_lifecycle())
        delivered = await self.check("bridge: send, delivery, read, headers", self._send_and_read())
        if delivered:
            await self.check("bridge: html sanitization", self._html_sanitization())
            await self.check("bridge: flags single and bulk", self._flags())
            await self.check("bridge: labels", self._labels())
            await self.check("bridge: reply, reply_all, forward, drafts of both", self._replies())
            await self.check("bridge: moves, copies, spam, trash, archive", self._moves())
            await self.check("bridge: bulk moves and bulk dry-run", self._bulk())
            await self.check("bridge: search_all_mail and read_thread", self._search_thread())
            await self.check("bridge: poll_mailbox", self._poll_mailbox())
            await self.check("bridge: unsubscribe", self._unsubscribe())
            await self.check("bridge: destructive guards", self._destructive_guards())
            await self.check("bridge: guarded empty_trash/empty_spam", self._empty_folders())

    async def _folder_lifecycle(self) -> None:
        original = f"Folders/{self.marker}-folder"
        renamed = f"Folders/{self.marker}-renamed"
        await self.call("create_folder", {"name": original})
        await self.call("rename_folder", {"name": original, "new_name": renamed})
        await self.call("subscribe_folder", {"name": renamed})
        again = await self.call("subscribe_folder", {"name": renamed})
        if again.get("changed") is not False:
            raise CheckFailure("repeated subscribe did not report changed=false")
        await self.call("unsubscribe_folder", {"name": renamed})
        await self.call("folder_status", {"name": renamed})
        await self.call("delete_folder", {"name": renamed, "confirm": True})

    async def _dry_run_send(self) -> None:
        subject = f"{self.marker} dry-run"
        preview = await self.call(
            "send_mail",
            self.with_sender({"to": self.args.recipient, "subject": subject, "text": "dry", "dry_run": True}),
        )
        for key in ("dry_run", "recipients", "subject"):
            if key not in preview:
                raise CheckFailure(f"dry_run preview missing {key}")
        await asyncio.sleep(3)
        sent = await self.search("Sent", query=subject)
        if sent:
            raise CheckFailure("dry_run send left a message in Sent")

    async def _draft_lifecycle(self) -> None:
        await self.call("create_folder", {"name": self.folder})
        subject = f"{self.marker} workflow"
        attachment = {
            "filename": "pwc-live-test.txt",
            "content_type": "text/plain",
            "content_base64": base64.b64encode(self.attachment).decode(),
        }
        await self.call(
            "create_draft",
            self.with_sender(
                {
                    "to": self.to_both(),
                    "subject": subject,
                    "text": f"draft {self.marker}",
                    "attachments": [attachment],
                }
            ),
        )
        drafts = await self.search("Drafts", attempts=10)
        if not drafts:
            raise CheckFailure("created draft did not appear")
        uid = drafts[0]["uid"]
        metadata = await self.call("inspect_attachments", {"message_id": uid, "folder": "Drafts"})
        downloaded = await self.call(
            "download_attachment",
            {"message_id": uid, "attachment_index": metadata[0]["index"], "folder": "Drafts"},
        )
        if base64.b64decode(downloaded["content_base64"]) != self.attachment:
            raise CheckFailure("downloaded attachment bytes differ from upload")
        await self.call(
            "update_draft",
            self.with_sender(
                {
                    "message_id": uid,
                    "to": self.to_both(),
                    "subject": subject,
                    "text": f"updated {self.marker}",
                    "attachments": [attachment],
                }
            ),
        )
        # update_draft appends the replacement before the old draft's deletion propagates
        # through Bridge, so wait for the count to settle instead of failing on first sight of 2.
        drafts = await self.search("Drafts", attempts=10)
        for _ in range(10):
            if len(drafts) == 1:
                break
            await asyncio.sleep(2)
            drafts = await self.search("Drafts", attempts=1)
        if len(drafts) != 1:
            raise CheckFailure(f"expected one replacement draft, found {len(drafts)}")
        throwaway = await self.call(
            "create_draft",
            self.with_sender({"to": self.args.recipient, "subject": f"{self.marker} delete-me", "text": "x"}),
        )
        deletable = await self.search("Drafts", attempts=10, query=f"{self.marker} delete-me")
        if deletable:
            await self.call("delete_draft", {"message_id": deletable[0]["uid"], "confirm": True})
        else:
            raise CheckFailure(f"throwaway draft did not appear: {throwaway}")
        sent = await self.call("send_draft", {"message_id": drafts[0]["uid"], "folder": "Drafts"})
        if not sent.get("sent") or not sent.get("draft_deleted"):
            raise CheckFailure("send_draft did not report send + draft cleanup")
        self._workflow_sent = True

    async def _send_and_read(self) -> None:
        if not getattr(self, "_workflow_sent", False):
            raise CheckFailure("nothing to deliver: the draft-lifecycle check failed before send_draft ran")
        inbox = await self.search("INBOX", attempts=20, query=f"{self.marker} workflow")
        if not inbox:
            raise CheckFailure("sent message did not arrive in INBOX (self-copy expected)")
        uid = inbox[0]["uid"]
        self._inbox_uid = uid
        message = await self.call("read_mail", {"message_id": uid, "folder": "INBOX"})
        if self.marker not in message.get("body", ""):
            raise CheckFailure("received body missing marker")
        if message.get("content_trust") != "untrusted":
            raise CheckFailure("read_mail result missing content_trust=untrusted")
        headers = await self.call("get_headers", {"message_id": uid, "folder": "INBOX"})
        raw_keys = {key.lower() for key in headers.get("headers", {})}
        if "message-id" not in raw_keys or "authentication" not in headers:
            raise CheckFailure("get_headers missing raw Message-ID header or parsed authentication")

    async def _html_sanitization(self) -> None:
        subject = f"{self.marker} html"
        result = await self.call(
            "send_mail",
            self.with_sender(
                {
                    "to": self.to_both(),
                    "subject": subject,
                    "text": "html test",
                    "html": f"<p>{self.marker}</p><script>alert(1)</script>",
                }
            ),
        )
        if result.get("html_sanitized") is not True:
            raise CheckFailure("active html was not reported as sanitized")
        arrived = await self.search("INBOX", attempts=15, query=subject)
        if arrived:
            body = await self.call("read_mail", {"message_id": arrived[0]["uid"], "folder": "INBOX"})
            if "<script>" in (body.get("body_html") or ""):
                raise CheckFailure("script tag survived sanitization in the delivered message")
        trusted = await self.call(
            "send_mail",
            self.with_sender(
                {
                    "to": self.args.recipient,
                    "subject": f"{self.marker} trusted html",
                    "text": "trusted",
                    "html": f"<p>{self.marker}</p>",
                    "trusted_html": True,
                    "dry_run": True,
                }
            ),
        )
        if trusted.get("html_sanitized"):
            raise CheckFailure("trusted_html=true still reported sanitization")

    async def _flags(self) -> None:
        uid = self._inbox_uid
        for name in ("mark_unread", "mark_read", "star_message", "unstar_message"):
            result = await self.call(name, {"message_id": uid, "folder": "INBOX"})
            if result.get("verified") is False:
                raise CheckFailure(f"{name} reported verified=false on INBOX")
        for name in ("bulk_mark_unread", "bulk_mark_read", "bulk_star", "bulk_unstar"):
            await self.call(name, {"message_ids": [uid], "folder": "INBOX"})

    async def _labels(self) -> None:
        uid = self._inbox_uid
        await self.call("create_folder", {"name": f"Labels/{self.label}"})
        labels = await self.call("list_labels")
        if not any(row["name"] == self.label for row in labels):
            raise EnvironmentIncomplete("created label not visible under Labels/ prefix")
        applied = await self.call("apply_label", {"message_id": uid, "label": self.label, "folder": "INBOX"})
        if applied.get("verified") is False:
            raise CheckFailure("apply_label reported verified=false")
        await self.call("remove_label", {"message_id": uid, "label": self.label, "folder": "INBOX"})

    async def _replies(self) -> None:
        uid = self._inbox_uid
        await self.call(
            "reply_mail",
            self.with_sender({"message_id": uid, "folder": "INBOX", "text": f"reply {self.marker}"}),
        )
        await self.call(
            "reply_all",
            self.with_sender({"message_id": uid, "folder": "INBOX", "text": f"reply all {self.marker}"}),
        )
        await self.call(
            "forward_mail",
            self.with_sender(
                {
                    "message_id": uid,
                    "folder": "INBOX",
                    "to": self.args.recipient,
                    "text": f"forward {self.marker}",
                    "include_original_attachments": True,
                }
            ),
        )
        preview = await self.call(
            "reply_mail",
            self.with_sender(
                {"message_id": uid, "folder": "INBOX", "text": f"dry reply {self.marker}", "dry_run": True}
            ),
        )
        if not preview.get("dry_run"):
            raise CheckFailure("reply_mail dry_run did not return a preview")
        await self.call(
            "draft_reply",
            self.with_sender({"message_id": uid, "folder": "INBOX", "text": f"draft reply {self.marker}"}),
        )
        await self.call(
            "draft_forward",
            self.with_sender(
                {"message_id": uid, "folder": "INBOX", "to": self.args.recipient, "text": f"draft fwd {self.marker}"}
            ),
        )
        for row in await self.search("Drafts", attempts=10):
            await self.call("delete_draft", {"message_id": row["uid"], "confirm": True})

    async def _moves(self) -> None:
        uid = self._inbox_uid
        await self.call("copy_message", {"message_id": uid, "folder": "INBOX", "destination_folder": self.folder})
        copied = await self.search(self.folder, attempts=10)
        if not copied:
            raise CheckFailure("copied message did not appear in the marker folder")
        await self.call("mark_spam", {"message_id": copied[0]["uid"], "folder": self.folder})
        spam = await self.search("Spam", attempts=10)
        if not spam:
            raise CheckFailure("message did not appear in Spam")
        await self.call("mark_not_spam", {"message_id": spam[0]["uid"], "destination_folder": self.folder})
        back = await self.search(self.folder, attempts=10)
        if not back:
            raise CheckFailure("message did not return from Spam")
        await self.call("trash_message", {"message_id": back[0]["uid"], "folder": self.folder})
        trash = await self.search("Trash", attempts=10)
        if not trash:
            raise CheckFailure("message did not appear in Trash")
        await self.call(
            "restore_message",
            {"message_id": trash[0]["uid"], "folder": "Trash", "destination_folder": self.folder},
        )
        restored = await self.search(self.folder, attempts=10)
        if not restored:
            raise CheckFailure("message did not restore from Trash")
        await self.call("archive_message", {"message_id": restored[0]["uid"], "folder": self.folder})
        archived = await self.search("Archive", attempts=10)
        if not archived:
            raise CheckFailure("message did not appear in Archive")
        await self.call(
            "move_message",
            {"message_id": archived[0]["uid"], "folder": "Archive", "destination_folder": self.folder},
        )

    async def _bulk(self) -> None:
        uids = [row["uid"] for row in await self.search(self.folder, attempts=10)]
        if not uids:
            raise CheckFailure("no marker messages available for bulk checks")
        preview = await self.call(
            "bulk_move",
            {"message_ids": uids, "folder": self.folder, "destination_folder": "Archive", "dry_run": True},
        )
        if not preview.get("dry_run"):
            raise CheckFailure("bulk_move dry_run did not return a preview")
        still = await self.search(self.folder)
        if len(still) != len(uids):
            raise CheckFailure("bulk_move dry_run mutated the folder")
        await self.call("bulk_copy", {"message_ids": uids, "folder": self.folder, "destination_folder": "Archive"})
        copies = [row["uid"] for row in await self.search("Archive", attempts=10)]
        if not copies:
            raise CheckFailure("bulk_copy produced no copies in Archive")
        await self.call("bulk_trash", {"message_ids": copies, "folder": "Archive"})
        trashed = [row["uid"] for row in await self.search("Trash", attempts=10)]
        if not trashed:
            raise CheckFailure("bulk_trash left nothing in Trash")
        await self.call("bulk_restore", {"message_ids": trashed, "folder": "Trash", "destination_folder": "Archive"})
        restored = [row["uid"] for row in await self.search("Archive", attempts=10)]
        if not restored:
            raise CheckFailure("bulk_restore returned nothing to Archive")
        await self.call("bulk_archive", {"message_ids": restored, "folder": "Archive"})
        remaining = [row["uid"] for row in await self.search("Archive", attempts=10)]
        if not remaining:
            # Proton's label semantics can leave Archive empty here; reseed with a copy so
            # the bulk_permanently_delete path is exercised on every run.
            seed = await self.search(self.folder, attempts=5)
            if not seed:
                raise CheckFailure("no marker message left to exercise bulk_permanently_delete")
            await self.call(
                "bulk_copy",
                {"message_ids": [seed[0]["uid"]], "folder": self.folder, "destination_folder": "Archive"},
            )
            remaining = [row["uid"] for row in await self.search("Archive", attempts=10)]
            if not remaining:
                raise CheckFailure("reseeded copy never appeared in Archive")
        deletion = await self.call(
            "bulk_permanently_delete",
            {"message_ids": remaining, "folder": "Archive", "dry_run": True},
        )
        if not deletion.get("dry_run"):
            raise CheckFailure("bulk_permanently_delete dry_run did not return a preview")
        await self.call(
            "bulk_permanently_delete",
            {"message_ids": remaining, "folder": "Archive", "confirm": True},
        )

    async def _live_inbox_uid(self) -> str:
        # Proton folders are labels: the moves check drags the one underlying message out of
        # INBOX via its "copy", so _inbox_uid goes stale. Re-resolve against what's really there.
        rows = await self.search("INBOX", attempts=5)
        if not rows:
            raise CheckFailure("no marker message left in INBOX for this check")
        return rows[0]["uid"]

    async def _search_thread(self) -> None:
        results = await self.call("search_all_mail", {"query": self.marker, "limit": 50})
        if not results.get("messages"):
            raise CheckFailure("search_all_mail found no marker messages")
        uid = await self._live_inbox_uid()
        thread = await self.call("read_thread", {"message_id": uid, "folder": "INBOX", "limit": 20})
        if not thread.get("messages"):
            raise CheckFailure("read_thread returned no messages")

    async def _poll_mailbox(self) -> None:
        cursor = f"{self.marker}-cursor"
        baseline = await self.call("poll_mailbox", {"folder": "INBOX", "cursor_name": cursor})
        if baseline.get("messages"):
            raise CheckFailure("first poll for a fresh cursor should baseline and return no messages")
        if "cursor_uid" not in baseline:
            raise CheckFailure("poll_mailbox response carries no cursor_uid")

    async def _unsubscribe(self) -> None:
        uid = await self._live_inbox_uid()
        result = await self.raw_call("unsubscribe", {"message_id": uid, "folder": "INBOX"})
        detail = " ".join(getattr(item, "text", "") for item in result.content)
        if not result.isError:
            return
        if "unsubscribe" in detail.lower() and ("no " in detail.lower() or "missing" in detail.lower()):
            raise EnvironmentIncomplete(
                "no List-Unsubscribe fixture message available; inject one (see TEST_PLAN.md) to cover the full path"
            )
        raise CheckFailure(f"unsubscribe failed unexpectedly: {detail}")

    async def _destructive_guards(self) -> None:
        await self.expect_error(
            "permanently_delete_message",
            {"message_id": self._inbox_uid, "folder": "INBOX", "confirm": False},
            "permanent deletion without confirm must be rejected",
        )
        await self.expect_error(
            "delete_folder",
            {"name": self.folder, "confirm": False},
            "folder deletion without confirm must be rejected",
        )
        await self.expect_error(
            "empty_trash",
            {"confirm": False},
            "empty_trash without confirm must be rejected",
        )

    async def _empty_folders(self) -> None:
        for tool, folder in (("empty_trash", "Trash"), ("empty_spam", "Spam")):
            status = await self.call("folder_status", {"name": folder})
            total = int(status.get("messages", 0))
            marked = await self.search(folder)
            if total != len(marked):
                raise EnvironmentIncomplete(f"{folder} holds non-marker mail; skipping {tool} to protect real data")
            preview = await self.call(tool, {"confirm": True, "dry_run": True})
            if not preview.get("dry_run"):
                raise CheckFailure(f"{tool} dry_run did not return a preview")
            await self.call(tool, {"confirm": True})

    # ------------------------------------------------------------------ simplelogin suite

    async def suite_simplelogin(self) -> None:
        await self.check("simplelogin: account, stats, mailboxes", self._sl_account())
        await self.check("simplelogin: alias lifecycle", self._sl_alias_lifecycle())
        await self.check("simplelogin: custom alias", self._sl_custom_alias())
        await self.check("simplelogin: poll_aliases", self._sl_poll())

    async def _sl_account(self) -> None:
        info = await self.call("simplelogin_user_info")
        if not info.get("is_premium"):
            raise EnvironmentIncomplete("SimpleLogin account is not premium; alias checks may hit caps")
        await self.call("simplelogin_stats")
        mailboxes = await self.call("simplelogin_list_mailboxes")
        if not mailboxes.get("mailboxes"):
            raise CheckFailure("no SimpleLogin mailboxes returned")
        self._sl_mailbox_id = mailboxes["mailboxes"][0]["id"]

    async def _sl_alias_lifecycle(self) -> None:
        created = await self.call("simplelogin_create_random_alias", {"note": self.marker})
        alias_id = created["id"]
        try:
            await self.call("simplelogin_get_alias", {"alias_id": alias_id})
            aliases = await self.call("simplelogin_list_aliases", {})
            listing = json.dumps(aliases)
            if str(alias_id) not in listing:
                raise CheckFailure("created alias missing from the alias list")
            await self.call("simplelogin_update_alias", {"alias_id": alias_id, "note": f"{self.marker} updated"})
            toggled = await self.call("simplelogin_toggle_alias", {"alias_id": alias_id})
            if "enabled" not in toggled:
                raise CheckFailure("toggle response missing enabled state")
            await self.call("simplelogin_toggle_alias", {"alias_id": alias_id})
            await self.call(
                "simplelogin_create_alias_contact",
                {"alias_id": alias_id, "contact": self.args.recipient},
            )
            contacts = await self.call("simplelogin_list_alias_contacts", {"alias_id": alias_id})
            if not contacts.get("contacts"):
                raise CheckFailure("created contact missing from the contact list")
            await self.expect_error(
                "simplelogin_delete_alias",
                {"alias_id": alias_id, "confirm": False},
                "alias deletion without confirm must be rejected",
            )
        finally:
            await self.call("simplelogin_delete_alias", {"alias_id": alias_id, "confirm": True})

    async def _sl_custom_alias(self) -> None:
        if not self.args.sl_signed_suffix:
            raise EnvironmentIncomplete(
                "no --sl-signed-suffix provided. PWC exposes no way to fetch alias options, so "
                "MCP-only users cannot call simplelogin_create_custom_alias at all — product gap; "
                "consider a simplelogin_get_alias_options tool"
            )
        created = await self.call(
            "simplelogin_create_custom_alias",
            {
                "alias_prefix": f"pwc-live-{int(time.time())}",
                "signed_suffix": self.args.sl_signed_suffix,
                "mailbox_ids": [self._sl_mailbox_id],
                "note": self.marker,
            },
        )
        await self.call("simplelogin_delete_alias", {"alias_id": created["id"], "confirm": True})

    async def _sl_poll(self) -> None:
        result = await self.call("poll_aliases", {"cursor_name": f"{self.marker}-aliases"})
        if "cursor_id" not in result:
            raise CheckFailure("poll_aliases response carries no cursor_id")

    # ------------------------------------------------------------------ watch suite

    async def suite_watch(self) -> None:
        if not (self.args.env_file and self.args.fixture_url and self.args.mint_secret):
            self._record(
                "watch: suite",
                INCOMPLETE,
                "needs --env-file (Bridge creds), --fixture-url, and --mint-secret",
                time.monotonic(),
            )
            return
        self._watch_dir = Path(tempfile.mkdtemp(prefix="pwc-watch-"))
        secret = self.args.webhook_secret or "pwc-live-secret"
        await self.fixture("PUT", "/fail-mode", {"enabled": False})
        await self.fixture("DELETE", "/deliveries")
        await self.check("watch: first run baselines quietly", self._watch_baseline(secret))
        await self.check("watch: webhook delivery with valid HMAC", self._watch_webhook(secret))
        await self.check("watch: dry-run holds the cursor", self._watch_dry_run(secret))
        await self.check("watch: dead-letter and replay", self._watch_dead_letter(secret))
        await self.check("watch: file and command sinks", self._watch_file_command_sinks())
        await self.check("watch: rules act and bad combos are rejected", self._watch_rules())

    async def fixture(self, method: str, path: str, body: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method,
                self.args.fixture_url.rstrip("/") + path,
                json=body,
                headers={"authorization": f"Bearer {self.args.mint_secret}"},
            )
            response.raise_for_status()
            return response.json()

    def _watch_cmd(self, *extra: str, secret: str | None = None) -> tuple[list[str], dict[str, str]]:
        env = dict(os.environ)
        env["PROTON_MCP_WATCH_STATE"] = str(self._watch_dir / "state.json")
        if secret:
            env["PROTON_MCP_WATCH_WEBHOOK_SECRET"] = secret
        cmd = [
            "proton-workflow-watch",
            "--env-file",
            self.args.env_file,
            "--folder",
            "INBOX",
            "--dead-letter",
            str(self._watch_dir / "dead-letter.jsonl"),
            *extra,
        ]
        return cmd, env

    async def _run_watch(self, *extra: str, secret: str | None = None, expect_rc: int = 0) -> str:
        cmd, env = self._watch_cmd(*extra, secret=secret)
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
        text = out.decode(errors="replace")
        if proc.returncode != expect_rc:
            raise CheckFailure(f"watcher exit {proc.returncode} (expected {expect_rc}): {text[-400:]}")
        return text

    async def _send_marker_mail(self, subject: str) -> None:
        await self.call(
            "send_mail",
            self.with_sender({"to": self.args.sender or self.args.recipient, "subject": subject, "text": subject}),
        )
        found = await self.search("INBOX", attempts=20, query=subject)
        if not found:
            raise CheckFailure(f"marker mail for the watcher did not arrive: {subject}")

    async def _watch_baseline(self, secret: str) -> None:
        webhook = self.args.fixture_url.rstrip("/") + "/webhook"
        await self._run_watch("--once", "--sink", "webhook", "--webhook-url", webhook, secret=secret)
        deliveries = await self.fixture("GET", "/deliveries")
        if deliveries:
            raise CheckFailure("baseline run delivered events from pre-existing mail")

    async def _watch_webhook(self, secret: str) -> None:
        subject = f"{self.marker} watch-webhook"
        await self._send_marker_mail(subject)
        webhook = self.args.fixture_url.rstrip("/") + "/webhook"
        await self._run_watch("--once", "--sink", "webhook", "--webhook-url", webhook, secret=secret)
        deliveries = await self.fixture("GET", "/deliveries")
        match = [d for d in deliveries if subject in d.get("body", "")]
        if not match:
            raise CheckFailure("watcher event never reached the webhook receiver")
        signature = match[0]["headers"].get("x-proton-signature", "")
        digest = hmac.new(secret.encode(), match[0]["body"].encode(), hashlib.sha256).hexdigest()
        if signature != f"sha256={digest}":
            raise CheckFailure("webhook HMAC signature did not verify")

    async def _watch_dry_run(self, secret: str) -> None:
        subject = f"{self.marker} watch-dryrun"
        await self._send_marker_mail(subject)
        await self.fixture("DELETE", "/deliveries")
        webhook = self.args.fixture_url.rstrip("/") + "/webhook"
        await self._run_watch("--dry-run", "--sink", "webhook", "--webhook-url", webhook, secret=secret)
        if await self.fixture("GET", "/deliveries"):
            raise CheckFailure("--dry-run delivered events")
        await self._run_watch("--once", "--sink", "webhook", "--webhook-url", webhook, secret=secret)
        deliveries = await self.fixture("GET", "/deliveries")
        if not any(subject in d.get("body", "") for d in deliveries):
            raise CheckFailure("event seen by --dry-run was lost instead of delivered on the next run")

    async def _watch_dead_letter(self, secret: str) -> None:
        subject = f"{self.marker} watch-deadletter"
        await self._send_marker_mail(subject)
        await self.fixture("PUT", "/fail-mode", {"enabled": True})
        await self.fixture("DELETE", "/deliveries")
        webhook = self.args.fixture_url.rstrip("/") + "/webhook"
        dead_letter = self._watch_dir / "dead-letter.jsonl"
        for _ in range(3):
            await self._run_watch(
                "--once",
                "--sink",
                "webhook",
                "--webhook-url",
                webhook,
                "--dead-letter-max-attempts",
                "1",
                secret=secret,
            )
            if dead_letter.exists():
                break
        if not dead_letter.exists():
            raise CheckFailure("failing deliveries never produced a dead-letter file")
        await self.fixture("PUT", "/fail-mode", {"enabled": False})
        await self.fixture("DELETE", "/deliveries")
        await self._run_watch("--replay-dead-letter", "--sink", "webhook", "--webhook-url", webhook, secret=secret)
        deliveries = await self.fixture("GET", "/deliveries")
        if not any(subject in d.get("body", "") for d in deliveries):
            raise CheckFailure("replay did not re-deliver the dead-lettered event")
        if dead_letter.exists() and dead_letter.read_text().strip():
            raise CheckFailure("dead-letter file still holds events after a successful replay")

    async def _watch_file_command_sinks(self) -> None:
        subject = f"{self.marker} watch-sinks"
        await self._send_marker_mail(subject)
        file_sink = self._watch_dir / "events.jsonl"
        await self._run_watch("--once", "--sink", "file", "--file", str(file_sink))
        if subject not in file_sink.read_text():
            raise CheckFailure("file sink did not record the event")
        subject2 = f"{self.marker} watch-command"
        await self._send_marker_mail(subject2)
        command_sink = self._watch_dir / "command.jsonl"
        # watch.py exec()s the command without a shell, so use tee -a instead of a redirect.
        await self._run_watch("--once", "--sink", "command", "--command", f"tee -a {command_sink}")
        if subject2 not in command_sink.read_text():
            raise CheckFailure("command sink did not record the event")

    async def _watch_rules(self) -> None:
        subject = f"{self.marker} watch-rule"
        rules = self._watch_dir / "rules.json"
        file_sink = self._watch_dir / "rule-events.jsonl"
        rules.write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "name": "live-rule",
                            "source": "mail",
                            "folder": "INBOX",
                            "subject": subject,
                            "actions": [{"type": "mark_read"}],
                        }
                    ]
                }
            )
        )
        # Cursors are keyed by rule name, so a never-seen rule baselines quietly on its
        # first run. Baseline it before sending the marker or the event is swallowed.
        await self._run_watch("--once", "--rules", str(rules), "--sink", "file", "--file", str(file_sink))
        await self._send_marker_mail(subject)
        await self._run_watch("--once", "--rules", str(rules), "--sink", "file", "--file", str(file_sink))
        if subject not in file_sink.read_text():
            raise CheckFailure("rule matching the marker subject did not emit an event")
        rows = await self.search("INBOX", query=subject)
        if rows and rows[0].get("unread") is True:
            raise CheckFailure("mark_read rule action did not act on the message")
        bad = self._watch_dir / "bad-rules.json"
        bad.write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "name": "bad-rule",
                            "source": "mail",
                            "folder": "INBOX",
                            "actions": [
                                {"type": "move", "folder": "Archive"},
                                {"type": "forward", "to": "x@example.com"},
                            ],
                        }
                    ]
                }
            )
        )
        cmd, env = self._watch_cmd("--once", "--rules", str(bad), "--sink", "file", "--file", str(file_sink))
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            raise CheckFailure("move+forward rule combination was accepted instead of rejected")

    # ------------------------------------------------------------------ safety suite

    async def suite_safety(self) -> None:
        if not self.args.env_file:
            self._record(
                "safety: suite", INCOMPLETE, "needs --env-file with Bridge creds on this host", time.monotonic()
            )
            return
        await self.check("safety: read-only mode", self._safety_read_only())
        await self.check("safety: sends disabled", self._safety_no_send())
        await self.check("safety: allowed action categories", self._safety_allowed_actions())
        await self.check("safety: read rate limit", self._safety_rate_limit())
        await self.check("safety: audit log written and redacted", self._safety_audit())

    async def _spawn_server(self, port: int, extra_env: dict[str, str]):
        env = dict(os.environ)
        # The env file pins allowed hosts to the main test port; spawned instances get their own.
        env["PROTON_MCP_HTTP_ALLOWED_HOSTS"] = f"127.0.0.1:{port}"
        env.update(extra_env)
        proc = await asyncio.create_subprocess_exec(
            "proton-workflow-connector",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--env-file",
            self.args.env_file,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(40):
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    await client.get(f"http://127.0.0.1:{port}/mcp")
                return proc
            except httpx.TransportError:
                if proc.returncode is not None:
                    raise CheckFailure(f"server exited {proc.returncode} during startup") from None
                await asyncio.sleep(0.5)
        proc.terminate()
        raise CheckFailure("spawned server never opened its port")

    async def _with_server(self, extra_env: dict[str, str], checks, token: str | None = None) -> None:
        port = 8790
        proc = await self._spawn_server(port, extra_env)
        url = f"http://127.0.0.1:{port}/mcp"
        try:
            if token is None:
                async with streamable_http_client(url) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await checks(session)
            else:
                headers = {"authorization": f"Bearer {token}"}
                async with httpx.AsyncClient(headers=headers, timeout=60) as http_client:
                    async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            await checks(session)
        finally:
            proc.terminate()
            await proc.wait()

    async def _session_call(self, session: ClientSession, name: str, arguments: dict[str, Any] | None = None):
        result = await asyncio.wait_for(session.call_tool(name, arguments or {}), timeout=self.args.timeout)
        return result

    async def _safety_read_only(self) -> None:
        async def checks(session: ClientSession) -> None:
            ok = await self._session_call(session, "list_folders")
            if ok.isError:
                raise CheckFailure("read-only mode blocked a read")
            write = await self._session_call(session, "create_folder", {"name": f"Folders/{self.marker}-ro"})
            if not write.isError:
                raise CheckFailure("read-only mode allowed a write")
            preview = await self._session_call(
                session,
                "send_mail",
                {"to": self.args.recipient or "x@example.com", "subject": "x", "text": "x", "dry_run": True},
            )
            if preview.isError:
                raise CheckFailure("read-only mode blocked a dry_run preview")

        await self._with_server({"PROTON_MCP_READ_ONLY": "true"}, checks)

    async def _safety_no_send(self) -> None:
        async def checks(session: ClientSession) -> None:
            send = await self._session_call(
                session,
                "send_mail",
                {"to": self.args.recipient or "x@example.com", "subject": "x", "text": "x"},
            )
            if not send.isError:
                raise CheckFailure("ALLOW_SEND=false still sent mail")
            folder = await self._session_call(session, "create_folder", {"name": f"Folders/{self.marker}-nosend"})
            if folder.isError:
                raise CheckFailure("ALLOW_SEND=false blocked a non-send write")
            await self._session_call(
                session, "delete_folder", {"name": f"Folders/{self.marker}-nosend", "confirm": True}
            )

        await self._with_server({"PROTON_MCP_ALLOW_SEND": "false"}, checks)

    async def _safety_allowed_actions(self) -> None:
        async def checks(session: ClientSession) -> None:
            ok = await self._session_call(session, "list_folders")
            if ok.isError:
                raise CheckFailure("read category was blocked despite being allowed")
            write = await self._session_call(session, "create_folder", {"name": f"Folders/{self.marker}-cat"})
            if not write.isError:
                raise CheckFailure("write category was allowed despite ALLOWED_ACTIONS=read")

        await self._with_server({"PROTON_MCP_ALLOWED_ACTIONS": "read"}, checks)

    async def _safety_rate_limit(self) -> None:
        # Rate limits apply per authenticated subject (docs/HOSTING.md), so an unauthenticated
        # instance ignores PROTON_MCP_RATE_LIMIT_* by design. Spawn an OAuth-enabled instance
        # against the fixture issuer and probe it with a minted token.
        if not (self.args.fixture_url and self.args.mint_secret):
            raise EnvironmentIncomplete("needs --fixture-url and --mint-secret to mint an OAuth token")
        audience = "http://127.0.0.1:8790/mcp"
        token = await self._mint(aud=audience, scope=f"{self.args.oauth_scope_base} mail.read")

        async def checks(session: ClientSession) -> None:
            rejected = False
            for _ in range(6):
                result = await self._session_call(session, "list_folders")
                if result.isError:
                    detail = " ".join(getattr(item, "text", "") for item in result.content).lower()
                    if "rate" in detail or "limit" in detail:
                        rejected = True
                        break
                    raise CheckFailure(f"unexpected error while probing the rate limit: {detail[:200]}")
            if not rejected:
                raise CheckFailure("read rate limit of 2/min never rejected a burst of 6 reads")

        await self._with_server(
            {
                "PROTON_MCP_RATE_LIMIT_READ": "2",
                "PROTON_MCP_OAUTH_ISSUER_URL": self.args.fixture_url.rstrip("/"),
                "PROTON_MCP_OAUTH_AUDIENCE": audience,
                "PROTON_MCP_OAUTH_RESOURCE_SERVER_URL": audience,
            },
            checks,
            token=token,
        )

    async def _safety_audit(self) -> None:
        audit = Path(tempfile.mkdtemp(prefix="pwc-audit-")) / "audit.jsonl"

        async def checks(session: ClientSession) -> None:
            # Reads are excluded from auditing by design; only writes produce records.
            await self._session_call(session, "list_folders")
            created = await self._session_call(session, "create_folder", {"name": f"Folders/{self.marker}-audit"})
            if created.isError:
                raise CheckFailure("could not create a folder to generate an audit record")
            await self._session_call(
                session, "delete_folder", {"name": f"Folders/{self.marker}-audit", "confirm": True}
            )

        await self._with_server({"PROTON_MCP_AUDIT_LOG": str(audit)}, checks)
        if not audit.exists():
            raise CheckFailure("audit log file was never created")
        lines = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
        if not lines:
            raise CheckFailure("audit log is empty after audited calls")
        if any(record.get("operation") == "list_folders" for record in lines):
            raise CheckFailure("audit log recorded a read operation; reads are excluded by design")
        text = audit.read_text().lower()
        for needle in ("password", "content_base64", "body"):
            if f'"{needle}"' in text:
                raise CheckFailure(f"audit log contains a {needle} field; expected redaction")

    # ------------------------------------------------------------------ oauth suite

    async def suite_oauth(self) -> None:
        if not (self.args.fixture_url and self.args.mint_secret):
            self._record("oauth: suite", INCOMPLETE, "needs --fixture-url and --mint-secret", time.monotonic())
            return
        base = self.args.oauth_scope_base
        audience = self.args.oauth_audience or self.args.url
        issuer = self.args.fixture_url.rstrip("/")
        await self.check("oauth: unauthenticated and malformed tokens rejected", self._oauth_reject_basics())
        await self.check(
            "oauth: expired, wrong-issuer, wrong-audience, bad-signature rejected",
            self._oauth_reject_claims(issuer, audience, base),
        )
        await self.check("oauth: scope ladder enforced", self._oauth_scope_ladder(issuer, audience, base))
        await self.check("oauth: admin scope satisfies everything", self._oauth_admin(issuer, audience, base))

    async def _mint(self, **claims: Any) -> str:
        result = await self.fixture("POST", "/mint", claims)
        return result["access_token"]

    async def _raw_mcp_status(self, token: str | None) -> int:
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pwc-live-acceptance", "version": "0"},
            },
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(self.args.url, json=body, headers=headers)
            return response.status_code

    async def _expect_rejected(self, token: str | None, why: str) -> None:
        status = await self._raw_mcp_status(token)
        if status not in (401, 403):
            raise CheckFailure(f"{why}: expected 401/403, got {status}")

    async def _oauth_reject_basics(self) -> None:
        await self._expect_rejected(None, "missing token")
        await self._expect_rejected("not-a-jwt", "malformed token")

    async def _oauth_reject_claims(self, issuer: str, audience: str, base: str) -> None:
        scope = f"{base} mail.read"
        await self._expect_rejected(await self._mint(aud=audience, scope=scope, expires_in=-60), "expired token")
        await self._expect_rejected(
            await self._mint(aud=audience, scope=scope, iss="https://wrong-issuer.example"), "wrong issuer"
        )
        await self._expect_rejected(
            await self._mint(aud="https://wrong-audience.example/mcp", scope=scope), "wrong audience"
        )
        await self._expect_rejected(await self._mint(aud=audience, scope=scope, wrong_key=True), "bad signature")
        await self._expect_rejected(await self._mint(aud=audience, scope="mail.read"), "missing base scope")

    async def _with_token(self, token: str, checks) -> None:
        headers = {"authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(headers=headers, timeout=60) as http_client:
            async with streamable_http_client(self.args.url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await checks(session)

    async def _oauth_scope_ladder(self, issuer: str, audience: str, base: str) -> None:
        async def read_checks(session: ClientSession) -> None:
            folders = await self._session_call(session, "list_folders")
            if folders.isError:
                raise CheckFailure("mail.read token could not read")
            created = await self._session_call(session, "create_folder", {"name": f"Folders/{self.marker}-oauth"})
            if not created.isError:
                raise CheckFailure("mail.read token was allowed to write")

        async def write_checks(session: ClientSession) -> None:
            created = await self._session_call(session, "create_folder", {"name": f"Folders/{self.marker}-oauth"})
            if created.isError:
                raise CheckFailure("mail.write token could not write")
            deleted = await self._session_call(
                session, "delete_folder", {"name": f"Folders/{self.marker}-oauth", "confirm": True}
            )
            if not deleted.isError:
                raise CheckFailure("mail.write token was allowed a destructive delete")

        async def sl_checks(session: ClientSession) -> None:
            info = await self._session_call(session, "simplelogin_user_info")
            if info.isError:
                raise CheckFailure("simplelogin.read token could not read SimpleLogin")
            alias = await self._session_call(session, "simplelogin_create_random_alias", {"note": self.marker})
            if not alias.isError:
                raise CheckFailure("simplelogin.read token was allowed an alias write")

        await self._with_token(await self._mint(aud=audience, scope=f"{base} mail.read"), read_checks)
        await self._with_token(await self._mint(aud=audience, scope=f"{base} mail.read mail.write"), write_checks)
        await self._with_token(await self._mint(aud=audience, scope=f"{base} simplelogin.read"), sl_checks)

    async def _oauth_admin(self, issuer: str, audience: str, base: str) -> None:
        async def checks(session: ClientSession) -> None:
            status = await self._session_call(session, "server_status")
            if status.isError:
                raise CheckFailure("admin token could not call server_status")
            created = await self._session_call(session, "create_folder", {"name": f"Folders/{self.marker}-admin"})
            if created.isError:
                raise CheckFailure("admin token could not write")
            deleted = await self._session_call(
                session, "delete_folder", {"name": f"Folders/{self.marker}-admin", "confirm": True}
            )
            if deleted.isError:
                raise CheckFailure("admin token could not delete")

        # Per docs/HOSTING.md every token carries the base scope; .admin is granted on top.
        await self._with_token(await self._mint(aud=audience, scope=f"{base} {base}.admin"), checks)

    # ------------------------------------------------------------------ cleanup + report

    async def suite_sweep(self) -> None:
        """Remove marker data left behind by any earlier run (e.g. one that died mid-cleanup)."""
        prefix = "PWC-live-"
        removed = 0
        folders = await self.call("list_folders")
        selectable = [
            row["name"]
            for row in folders
            if "\\Noselect" not in row.get("flags", []) and "\\All" not in row.get("flags", [])
        ]
        for folder in selectable:
            rows: dict[str, dict[str, Any]] = {}
            for style in ({"query": prefix}, {"subject": prefix}):
                try:
                    found = await self.call("search_mail", {"folder": folder, "limit": 100, **style})
                except Exception:
                    continue
                rows.update({row["uid"]: row for row in found})
            for row in rows.values():
                result = await self.raw_call(
                    "permanently_delete_message",
                    {"message_id": row["uid"], "folder": folder, "confirm": True},
                )
                removed += int(not result.isError)
        for name in [row["name"] for row in folders if prefix in row["name"]]:
            await self.raw_call("delete_folder", {"name": name, "confirm": True})
        leftovers = await self.call("search_all_mail", {"query": prefix, "limit": 50})
        remaining = len(leftovers.get("messages", []))
        status = PASS if remaining == 0 else FAIL
        self._record(
            "sweep: stale marker data removed",
            status,
            f"removed {removed}, remaining {remaining}" if remaining else "",
            time.monotonic(),
        )

    async def cleanup(self) -> int:
        print("cleanup: removing marker data", flush=True)
        removed = 0
        try:
            folders = await self.call("list_folders")
        except Exception:
            folders = []
        names = [
            row["name"]
            for row in folders
            if "\\Noselect" not in row.get("flags", []) and "\\All" not in row.get("flags", [])
        ]

        async def sweep_round() -> None:
            nonlocal removed
            for folder in names:
                # TEXT search can miss messages SUBJECT search finds (and vice versa), so run both.
                rows: dict[str, dict[str, Any]] = {}
                for style in ({"query": self.marker}, {"subject": self.marker}):
                    try:
                        found = await self.call("search_mail", {"folder": folder, "limit": 100, **style})
                    except Exception:
                        continue
                    rows.update({row["uid"]: row for row in found})
                for row in rows.values():
                    try:
                        result = await self.raw_call(
                            "permanently_delete_message",
                            {"message_id": row["uid"], "folder": folder, "confirm": True},
                        )
                        removed += int(not result.isError)
                    except Exception:
                        pass

        for _ in range(3):
            await sweep_round()
            await asyncio.sleep(2)
        for folder in (
            self.folder,
            f"Folders/{self.marker}-folder",
            f"Folders/{self.marker}-renamed",
            f"Labels/{self.label}",
        ):
            try:
                await self.raw_call("delete_folder", {"name": folder, "confirm": True})
            except Exception:
                pass
        # Mail sent to self can land minutes after its check finished, so keep sweeping
        # stragglers instead of failing the moment the first search still sees them.
        started = time.monotonic()
        try:
            while True:
                leftovers = await self.call("search_all_mail", {"query": self.marker, "limit": 50})
                if not leftovers.get("messages"):
                    self._record("cleanup: final marker sweep", PASS, "", started)
                    break
                if time.monotonic() - started > 180:
                    self._record("cleanup: final marker sweep", FAIL, "marker messages remain", started)
                    break
                await asyncio.sleep(10)
                await sweep_round()
        except Exception as exc:
            self._record("cleanup: final marker sweep", FAIL, str(exc), started)
        return removed

    def coverage(self, suites: list[str]) -> dict[str, Any]:
        required: set[str] = set()
        for suite in suites:
            required |= SUITE_TOOLS.get(suite, set())
        available = set(self.tools_available or [])
        if available:
            required &= available
        missing = sorted(required - self.tools_called)
        out_of_scope = sorted(available - required - self.tools_called) if available else []
        return {
            "tools_called": sorted(self.tools_called),
            "required_not_called": missing,
            "out_of_scope": out_of_scope,
        }

    def write_report(self, suites: list[str]) -> Path:
        coverage = self.coverage(suites)
        statuses = [entry["status"] for entry in self.checks]
        verdict = PASS
        if FAIL in statuses or coverage["required_not_called"]:
            verdict = FAIL
        elif INCOMPLETE in statuses:
            verdict = INCOMPLETE
        report = {
            "marker": self.marker,
            "suites": suites,
            "verdict": verdict,
            "checks": self.checks,
            "coverage": coverage,
        }
        directory = Path(self.args.report_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.marker}.json"
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"\nverdict: {verdict}")
        if coverage["required_not_called"]:
            print("required tools never exercised: " + ", ".join(coverage["required_not_called"]))
        print(f"report: {path}")
        return path

    # ------------------------------------------------------------------ entry

    async def run(self, suites: list[str]) -> int:
        if "oauth" in suites and len(suites) == 1:
            # The public endpoint requires tokens, so the shared unauthenticated session
            # used by the other suites cannot exist here.
            await self.suite_oauth()
        elif suites == ["sweep"]:
            async with streamable_http_client(self.args.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    self.session = session
                    await session.initialize()
                    await self.suite_sweep()
        else:
            async with streamable_http_client(self.args.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    self.session = session
                    await session.initialize()
                    try:
                        await self.suite_env_gate()
                        if "bridge" in suites:
                            await self.suite_bridge()
                        if "simplelogin" in suites:
                            await self.suite_simplelogin()
                        if "watch" in suites:
                            await self.suite_watch()
                    finally:
                        if "bridge" in suites or "watch" in suites:
                            await self.cleanup()
            if "safety" in suites:
                await self.suite_safety()
        self.write_report(suites)
        statuses = [entry["status"] for entry in self.checks]
        return 1 if FAIL in statuses else 0


def _redact(text: str) -> str:
    return text[:300]


def _describe(exc: BaseException) -> str:
    """Flatten ExceptionGroups (anyio task groups) into the underlying error messages."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(sub) for sub in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Marker-scoped live acceptance suites for PWC.")
    parser.add_argument(
        "suite", choices=["env-gate", "bridge", "simplelogin", "watch", "safety", "oauth", "sweep", "all"]
    )
    parser.add_argument("--url", required=True, help="Streamable HTTP MCP endpoint under test")
    parser.add_argument("--sender", help="Test sender address configured in Bridge")
    parser.add_argument("--recipient", help="Outside mailbox that receives test messages")
    parser.add_argument("--timeout", type=float, default=45.0, help="Per-tool timeout in seconds")
    parser.add_argument(
        "--sl-signed-suffix",
        help="Signed suffix from the SimpleLogin alias options API, for the custom-alias check",
    )
    parser.add_argument(
        "--env-file",
        help="Bridge credentials env file; required by the watch and safety suites (Bridge host only)",
    )
    parser.add_argument("--fixture-url", help="Base URL of the deployed test fixture Worker")
    parser.add_argument("--mint-secret", help="MINT_SECRET of the fixture Worker")
    parser.add_argument("--webhook-secret", help="HMAC secret for watcher webhook checks")
    parser.add_argument("--oauth-audience", help="Expected audience for minted tokens (defaults to --url)")
    parser.add_argument("--oauth-scope-base", default="proton-workflow-connector", help="Base OAuth scope name")
    parser.add_argument(
        "--report-dir",
        default="~/.local/state/proton-workflow-connector/live-tests",
        help="Where redacted JSON reports are written",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.suite == "all":
        suites = ["bridge", "simplelogin"]
        if args.env_file:
            suites += ["watch", "safety"]
    else:
        suites = [args.suite]
    if {"bridge", "watch"} & set(suites) and not args.recipient:
        raise SystemExit("--recipient is required for the bridge and watch suites")
    harness = Harness(args)
    raise SystemExit(asyncio.run(harness.run(suites)))


if __name__ == "__main__":
    main()

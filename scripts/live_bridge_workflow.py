#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class LiveWorkflow:
    def __init__(self, *, url: str, sender: str | None, recipient: str, timeout: float) -> None:
        self.url = url
        self.sender = sender
        self.recipient = recipient
        self.timeout = timeout
        self.marker = f"MCP-v1-live-{int(time.time())}"
        self.folder = f"Folders/{self.marker}"
        self.attachment = f"attachment {self.marker}\n".encode()
        self.passed: list[str] = []

    async def raw_call(self, session: ClientSession, name: str, arguments: dict[str, Any] | None = None):
        return await asyncio.wait_for(session.call_tool(name, arguments or {}), timeout=self.timeout)

    async def call(self, session: ClientSession, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return self._result_value(await self.raw_call(session, name, arguments))

    async def search(self, session: ClientSession, folder: str, *, attempts: int = 1) -> list[dict[str, Any]]:
        for _ in range(attempts):
            rows = await self.call(
                session,
                "search_mail",
                {"folder": folder, "query": self.marker, "limit": 100},
            )
            if rows:
                return rows
            await asyncio.sleep(2)
        return []

    def record(self, name: str) -> None:
        self.passed.append(name)
        print(f"passed: {name}", flush=True)

    def with_sender(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.sender:
            arguments["from_address"] = self.sender
        return arguments

    async def run(self) -> None:
        async with streamable_http_client(self.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                try:
                    await self._folder_workflow(session)
                    await self._mail_workflow(session)
                finally:
                    print("cleanup: waiting for delivery and removing marked test data", flush=True)
                    removed = await self._cleanup(session)
                    print(f"cleanup: removed {removed} matching message records", flush=True)
        print(json.dumps({"marker": self.marker, "passed": self.passed}, sort_keys=True))

    async def _folder_workflow(self, session: ClientSession) -> None:
        original = f"Folders/{self.marker}-folder"
        renamed = f"Folders/{self.marker}-renamed"
        await self.call(session, "create_folder", {"name": original})
        await self.call(session, "rename_folder", {"name": original, "new_name": renamed})
        await self.call(session, "subscribe_folder", {"name": renamed})
        repeated = await self.call(session, "subscribe_folder", {"name": renamed})
        if repeated.get("changed") is not False:
            raise RuntimeError("Repeated folder subscription did not report changed=false")
        await self.call(session, "unsubscribe_folder", {"name": renamed})
        repeated = await self.call(session, "unsubscribe_folder", {"name": renamed})
        if repeated.get("changed") is not False:
            raise RuntimeError("Repeated folder unsubscription did not report changed=false")
        await self.call(session, "folder_status", {"name": renamed})
        await self.call(session, "delete_folder", {"name": renamed, "confirm": True})
        self.record("folder create, rename, subscribe, status, and delete")

    async def _mail_workflow(self, session: ClientSession) -> None:
        await self.call(session, "create_folder", {"name": self.folder})
        subject = f"{self.marker} complete workflow"
        attachment = {
            "filename": "mcp-v1-test.txt",
            "content_type": "text/plain",
            "content_base64": base64.b64encode(self.attachment).decode(),
        }
        await self.call(
            session,
            "create_draft",
            self.with_sender(
                {
                    "to": self.recipient,
                    "subject": subject,
                    "text": f"draft {self.marker}",
                    "attachments": [attachment],
                }
            ),
        )
        drafts = await self.search(session, "Drafts", attempts=10)
        if not drafts:
            raise RuntimeError("Created draft did not appear")
        metadata = await self.call(
            session,
            "inspect_attachments",
            {"message_id": drafts[0]["uid"], "folder": "Drafts"},
        )
        downloaded = await self.call(
            session,
            "download_attachment",
            {
                "message_id": drafts[0]["uid"],
                "attachment_index": metadata[0]["index"],
                "folder": "Drafts",
            },
        )
        if base64.b64decode(downloaded["content_base64"]) != self.attachment:
            raise RuntimeError("Downloaded draft attachment did not match the upload")
        await self.call(
            session,
            "update_draft",
            self.with_sender(
                {
                    "message_id": drafts[0]["uid"],
                    "to": self.recipient,
                    "subject": subject,
                    "text": f"updated {self.marker}",
                    "attachments": [attachment],
                }
            ),
        )
        drafts = await self.search(session, "Drafts", attempts=10)
        if len(drafts) != 1:
            raise RuntimeError(f"Expected one replacement draft, found {len(drafts)}")
        self.record("draft create, update, and attachment round trip")

        sent = await self.call(
            session,
            "send_draft",
            {"message_id": drafts[0]["uid"], "folder": "Drafts"},
        )
        if not sent.get("sent") or not sent.get("draft_deleted"):
            raise RuntimeError("Draft send did not report complete cleanup")
        self.record("send draft")

        inbox = await self.search(session, "INBOX", attempts=20)
        if not inbox:
            raise RuntimeError("Sent test message did not arrive in INBOX")
        uid = inbox[0]["uid"]
        message = await self.call(session, "read_mail", {"message_id": uid, "folder": "INBOX"})
        if self.marker not in message.get("body", ""):
            raise RuntimeError("Received message body did not contain the test marker")
        received_attachments = await self.call(
            session,
            "inspect_attachments",
            {"message_id": uid, "folder": "INBOX"},
        )
        if not received_attachments:
            raise RuntimeError("Received message did not contain the test attachment")
        self.record("delivery, read, and received attachment inspection")

        for name in ("mark_unread", "mark_read", "star_message", "unstar_message"):
            await self.call(session, name, {"message_id": uid, "folder": "INBOX"})
        for name in ("bulk_mark_unread", "bulk_mark_read", "bulk_star", "bulk_unstar"):
            await self.call(session, name, {"message_ids": [uid], "folder": "INBOX"})
        self.record("single and bulk flags")

        await self.call(
            session,
            "reply_mail",
            self.with_sender({"message_id": uid, "folder": "INBOX", "text": f"reply {self.marker}"}),
        )
        await self.call(
            session,
            "reply_all",
            self.with_sender(
                {
                    "message_id": uid,
                    "folder": "INBOX",
                    "text": f"reply all {self.marker}",
                }
            ),
        )
        await self.call(
            session,
            "forward_mail",
            self.with_sender(
                {
                    "message_id": uid,
                    "folder": "INBOX",
                    "to": self.recipient,
                    "text": f"forward {self.marker}",
                    "include_original_attachments": True,
                }
            ),
        )
        self.record("reply, reply all, and forward")

        await self.call(
            session,
            "copy_message",
            {"message_id": uid, "folder": "INBOX", "destination_folder": self.folder},
        )
        copied = await self.search(session, self.folder, attempts=10)
        if not copied:
            raise RuntimeError("Copied message did not appear in the test folder")
        self.record("copy message")

        await self.call(session, "mark_spam", {"message_id": copied[0]["uid"], "folder": self.folder})
        spam = await self.search(session, "Spam", attempts=10)
        if not spam:
            raise RuntimeError("Message did not appear in Spam")
        await self.call(
            session,
            "mark_not_spam",
            {"message_id": spam[0]["uid"], "destination_folder": self.folder},
        )
        restored = await self.search(session, self.folder, attempts=10)
        if not restored:
            raise RuntimeError("Message did not return from Spam")
        self.record("spam and not-spam moves")

        await self.call(session, "trash_message", {"message_id": restored[0]["uid"], "folder": self.folder})
        trash = await self.search(session, "Trash", attempts=10)
        if not trash:
            raise RuntimeError("Message did not appear in Trash")
        await self.call(
            session,
            "restore_message",
            {"message_id": trash[0]["uid"], "folder": "Trash", "destination_folder": self.folder},
        )
        self.record("trash and restore moves")

        all_results = await self.call(session, "search_all_mail", {"query": self.marker, "limit": 50})
        if not all_results["messages"]:
            raise RuntimeError("All-folder search returned no messages")
        thread = await self.call(
            session,
            "read_thread",
            {"message_id": uid, "folder": "INBOX", "limit": 20},
        )
        if not thread["messages"]:
            raise RuntimeError("Thread read returned no messages")
        self.record("all-folder search and thread read")

        rejected = await self.raw_call(
            session,
            "permanently_delete_message",
            {"message_id": uid, "folder": "INBOX", "confirm": False},
        )
        if not rejected.isError:
            raise RuntimeError("Permanent deletion was accepted without confirmation")
        self.record("destructive confirmation guard")

    async def _cleanup(self, session: ClientSession) -> int:
        await asyncio.sleep(5)
        removed = 0
        try:
            folders = await self.call(session, "list_folders")
        except Exception:
            folders = []
        names = [
            row["name"]
            for row in folders
            if "\\Noselect" not in row.get("flags", []) and "\\All" not in row.get("flags", [])
        ]
        for _ in range(3):
            for folder in names:
                try:
                    rows = await self.search(session, folder)
                except Exception:
                    continue
                for row in rows:
                    try:
                        result = await self.raw_call(
                            session,
                            "permanently_delete_message",
                            {"message_id": row["uid"], "folder": folder, "confirm": True},
                        )
                        removed += int(not result.isError)
                    except Exception:
                        pass
            await asyncio.sleep(2)
        for folder in (self.folder, f"Folders/{self.marker}-folder", f"Folders/{self.marker}-renamed"):
            try:
                await self.raw_call(session, "delete_folder", {"name": folder, "confirm": True})
            except Exception:
                pass
        return removed

    @staticmethod
    def _result_value(result) -> Any:
        if result.isError:
            detail = " ".join(getattr(item, "text", "") for item in result.content)
            raise RuntimeError(detail)
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured.get("result", structured) if isinstance(structured, dict) else structured
        for item in result.content:
            if getattr(item, "type", None) == "text":
                return json.loads(item.text)
        raise RuntimeError("Tool returned no decodable content")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a destructive, self-cleaning live Bridge workflow.")
    parser.add_argument("--url", required=True, help="Streamable HTTP MCP endpoint")
    parser.add_argument("--sender", help="Configured Bridge sender address. Omit to use the server default sender.")
    parser.add_argument("--recipient", required=True, help="Mailbox that can receive the test messages")
    parser.add_argument("--timeout", type=float, default=45.0, help="Per-tool timeout in seconds")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workflow = LiveWorkflow(url=args.url, sender=args.sender, recipient=args.recipient, timeout=args.timeout)
    asyncio.run(workflow.run())


if __name__ == "__main__":
    main()

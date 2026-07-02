"""Phase 1 transport checks run from a LAN peer (PC1) against the server under test.

Covers the TEST_PLAN Phase 1 rows that must originate off-host: list tools, server_status,
one read, one dry-run write, one guarded destructive rejection, and a wrong-Host rejection.
With --token it exercises the hosted-HTTPS row instead (OAuth on, via the tunnel hostname).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

EXPECTED_TOOLS = 68


def result_json(result) -> Any:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    for item in result.content:
        if getattr(item, "text", None):
            return json.loads(item.text)
    raise AssertionError("tool returned no decodable content")


async def run_checks(url: str, token: str | None) -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'pass' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    headers = {"authorization": f"Bearer {token}"} if token else {}
    async with streamable_http_client(url, headers=headers or None, timeout=60) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            check(
                f"list tools ({EXPECTED_TOOLS} expected)",
                len(tools.tools) == EXPECTED_TOOLS,
                f"got {len(tools.tools)}",
            )
            annotated = sum(1 for tool in tools.tools if tool.annotations is not None)
            check("tool annotations present", annotated == len(tools.tools), f"{annotated} annotated")
            status = result_json(await session.call_tool("server_status", {}))
            check("server_status", "services" in status or "bridge" in json.dumps(status).lower())
            folders = result_json(await session.call_tool("list_folders", {}))
            check("list_folders read", isinstance(folders, list) and len(folders) > 0)
            dry = result_json(
                await session.call_tool(
                    "send_mail",
                    {
                        "to": "dryrun@example.com",
                        "subject": "transport check",
                        "text": "dry run",
                        "dry_run": True,
                    },
                )
            )
            check("send_mail dry_run", dry.get("dry_run") is True and not dry.get("sent"))
            guarded = await session.call_tool(
                "permanently_delete_message", {"message_id": "1", "folder": "INBOX", "confirm": False}
            )
            check("guarded destructive rejected without confirm", guarded.isError)

    # Wrong Host header must be rejected (DNS-rebinding protection).
    initialize_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}},
    }
    bad_host_headers = {
        **headers,
        "host": "evil.example.com",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30, verify=True) as client:
        response = await client.post(url, json=initialize_body, headers=bad_host_headers)
        rejected = response.status_code in (400, 403, 421)
        print(f"{'pass' if rejected else 'FAIL'}: wrong Host header rejected (HTTP {response.status_code})")
        if not rejected:
            failures.append("wrong host")

    print("verdict:", "pass" if not failures else f"fail ({', '.join(failures)})")
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="MCP endpoint, e.g. http://192.168.1.167:8766/mcp")
    parser.add_argument("--token", help="Bearer token for the hosted OAuth row")
    args = parser.parse_args()
    return asyncio.run(run_checks(args.url, args.token))


if __name__ == "__main__":
    sys.exit(main())

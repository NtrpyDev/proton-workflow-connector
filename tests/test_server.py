import asyncio
import json

import pytest

from proton_mail_mcp.config import Settings
from proton_mail_mcp.server import _require_confirmation, _validate_http_security, build_server
from proton_mail_mcp.watch import CursorStore


class PollingMailClient:
    def __init__(self, cursor_uid: int = 5, uid_validity: int = 7) -> None:
        self.cursor_uid = cursor_uid
        self.uid_validity = uid_validity

    def poll_folder(self, **kwargs):
        last_uid = kwargs["last_uid"]
        baseline = kwargs["uid_validity"] is None and last_uid == 0
        messages = [] if baseline else [{"uid": str(self.cursor_uid), "message_id": "<five@example.com>"}]
        return {
            "folder": kwargs["folder"],
            "messages": messages,
            "cursor_uid": self.cursor_uid,
            "uid_validity": self.uid_validity,
            "baseline": baseline,
            "reset": False,
            "more": False,
        }


async def _call_json(server, name: str, arguments: dict) -> dict:
    content = await server.call_tool(name, arguments)
    return json.loads(content[0].text)


def test_server_registers_complete_tool_surface():
    async def list_names():
        return {tool.name for tool in await build_server().list_tools()}

    names = asyncio.run(list_names())

    assert len(names) == 69
    assert {
        "download_attachment",
        "reply_mail",
        "reply_all",
        "forward_mail",
        "search_all_mail",
        "poll_mailbox",
        "ack_mailbox",
        "poll_aliases",
        "send_draft",
        "empty_trash",
        "server_status",
        "get_headers",
        "draft_reply",
        "draft_forward",
        "list_labels",
        "apply_label",
        "remove_label",
        "unsubscribe",
    } <= names


def test_poll_peek_does_not_advance_until_checkpoint_is_acknowledged(tmp_path):
    state_path = tmp_path / "watch-state.json"
    server = build_server(
        settings=Settings(watch_state_path=str(state_path)),
        mail_client=PollingMailClient(),
    )

    peek = asyncio.run(_call_json(server, "poll_mailbox", {"cursor_name": "sorter", "advance": False}))

    assert peek["baseline"] is True
    assert peek["previous_cursor_uid"] == 0
    assert CursorStore.load(state_path).get("sorter") == (0, None)

    arguments = {
        "cursor_name": "sorter",
        "cursor_uid": peek["cursor_uid"],
        "uid_validity": peek["uid_validity"],
        "expected_cursor_uid": peek["previous_cursor_uid"],
        "expected_uid_validity": peek["previous_uid_validity"],
    }
    first = asyncio.run(_call_json(server, "ack_mailbox", arguments))
    second = asyncio.run(_call_json(server, "ack_mailbox", arguments))

    assert first["advanced"] is True
    assert second["advanced"] is False
    assert CursorStore.load(state_path).get("sorter") == (5, 7)


def test_poll_peek_replays_batch_until_acknowledged(tmp_path):
    state_path = tmp_path / "watch-state.json"
    seed = CursorStore.load(state_path)
    seed.set("sorter", cursor_uid=4, uid_validity=7)
    seed.save()
    server = build_server(
        settings=Settings(watch_state_path=str(state_path)),
        mail_client=PollingMailClient(),
    )

    first = asyncio.run(_call_json(server, "poll_mailbox", {"cursor_name": "sorter", "advance": False}))
    second = asyncio.run(_call_json(server, "poll_mailbox", {"cursor_name": "sorter", "advance": False}))

    assert first["messages"] == second["messages"]
    assert first["previous_cursor_uid"] == second["previous_cursor_uid"] == 4
    assert CursorStore.load(state_path).get("sorter") == (4, 7)


def test_destructive_tools_expose_confirmation_field():
    async def schemas():
        return {tool.name: tool.inputSchema for tool in await build_server().list_tools()}

    tool_schemas = asyncio.run(schemas())

    for name in {
        "delete_folder",
        "delete_draft",
        "permanently_delete_message",
        "bulk_permanently_delete",
        "empty_trash",
        "empty_spam",
        "simplelogin_delete_alias",
    }:
        assert "confirm" in tool_schemas[name]["properties"]


def test_safety_parameters_are_exposed_in_tool_schemas():
    async def schemas():
        return {tool.name: tool.inputSchema for tool in await build_server().list_tools()}

    tool_schemas = asyncio.run(schemas())

    for name in {"send_mail", "reply_mail", "reply_all", "forward_mail"}:
        assert "dry_run" in tool_schemas[name]["properties"]
        assert "trusted_html" in tool_schemas[name]["properties"]
    for name in {"create_draft", "draft_reply", "draft_forward", "update_draft"}:
        assert "trusted_html" in tool_schemas[name]["properties"]
    for name in {
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
    }:
        assert "dry_run" in tool_schemas[name]["properties"]


def test_confirmation_is_required():
    with pytest.raises(ValueError, match="confirm=true"):
        _require_confirmation(False, "delete")
    _require_confirmation(True, "delete")


def test_nonlocal_http_requires_allowed_hosts_and_authentication():
    with pytest.raises(RuntimeError, match="ALLOWED_HOSTS"):
        _validate_http_security("streamable-http", "0.0.0.0", Settings())
    with pytest.raises(RuntimeError, match="OAuth"):
        _validate_http_security(
            "streamable-http",
            "0.0.0.0",
            Settings(http_allowed_hosts=("mail.example.com:*",)),
        )


def test_hosted_oauth_requires_https():
    settings = Settings(
        http_allowed_hosts=("mail.example.com:*",),
        oauth_issuer_url="https://issuer.example.com",
        oauth_audience="https://mail.example.com/mcp",
        oauth_resource_server_url="http://mail.example.com/mcp",
    )

    with pytest.raises(RuntimeError, match="HTTPS"):
        _validate_http_security("streamable-http", "0.0.0.0", settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"oauth_issuer_url": "http://issuer.example.com"},
        {"oauth_jwks_url": "http://issuer.example.com/jwks"},
    ],
)
def test_hosted_oauth_rejects_insecure_issuer_and_jwks(overrides):
    values = {
        "http_allowed_hosts": ("mail.example.com:*",),
        "oauth_issuer_url": "https://issuer.example.com",
        "oauth_audience": "https://mail.example.com/mcp",
        "oauth_resource_server_url": "https://mail.example.com/mcp",
        **overrides,
    }

    with pytest.raises(RuntimeError, match="HTTPS"):
        _validate_http_security("streamable-http", "0.0.0.0", Settings(**values))


def test_hosted_server_configures_oauth_and_transport_security():
    settings = Settings(
        http_allowed_hosts=("mail.example.com",),
        http_allowed_origins=("https://client.example.com",),
        oauth_issuer_url="https://issuer.example.com",
        oauth_audience="https://mail.example.com/mcp",
        oauth_resource_server_url="https://mail.example.com/mcp",
    )

    server = build_server(settings=settings, host="127.0.0.1", port=8765, enable_http_auth=True)

    assert server.settings.auth.issuer_url.unicode_string() == "https://issuer.example.com/"
    assert server.settings.transport_security.allowed_hosts == ["mail.example.com"]
    assert server.settings.transport_security.allowed_origins == ["https://client.example.com"]


def test_tools_expose_mcp_annotations():
    async def annotations():
        return {tool.name: tool.annotations for tool in await build_server().list_tools()}

    ann = asyncio.run(annotations())
    assert ann["read_mail"].readOnlyHint is True
    assert ann["read_mail"].idempotentHint is True
    assert ann["permanently_delete_message"].destructiveHint is True
    assert ann["send_mail"].readOnlyHint is False
    assert ann["send_mail"].openWorldHint is True


def test_tool_functions_run_off_the_event_loop():
    """A synchronous tool body would block the event loop for every session if Bridge hangs."""
    import inspect

    server = build_server()
    for name, tool in server._tool_manager._tools.items():
        assert tool.is_async, f"{name} would run on the event loop"
        assert inspect.iscoroutinefunction(tool.fn), f"{name} is not wrapped as a coroutine"


def test_operations_hit_a_wall_clock_deadline(monkeypatch):
    """A wedged-but-dribbling Bridge session must surface as a clear error, not a silent hang."""
    import asyncio
    import time

    from proton_mail_mcp.server import _run_tools_in_worker_threads

    class FakeTool:
        def fn(self):  # pragma: no cover - replaced below
            pass

        is_async = False

    def hang():
        time.sleep(5)

    tool = FakeTool()
    tool.fn = hang

    class FakeMCP:
        class _tool_manager:
            _tools = {"hang": tool}

    _run_tools_in_worker_threads(FakeMCP, deadline=0.2)

    with pytest.raises(RuntimeError, match="Bridge is not responding"):
        asyncio.run(tool.fn())


def test_fast_operations_pass_under_the_deadline():
    import asyncio

    from proton_mail_mcp.server import _run_tools_in_worker_threads

    class FakeTool:
        fn = staticmethod(lambda: "ok")
        is_async = False

    tool = FakeTool()

    class FakeMCP:
        class _tool_manager:
            _tools = {"fast": tool}

    _run_tools_in_worker_threads(FakeMCP, deadline=5.0)
    assert asyncio.run(tool.fn()) == "ok"

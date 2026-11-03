"""Wave 6.2: the MCP server is a pure HTTP client of the wall.

A threaded fake wall records every request so each tool can be checked for the op it sends,
the args it builds and the X-Wall-Key / X-Wall-Actor headers it carries. Nothing here opens
the wall's store; the module under test must not either (test_no_store_or_engine_imports).
"""
import ast
import asyncio
import json
import os
import pathlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from pcwall import mcp_server
from pcwall.mcp_server import TOOL_NAMES, WallClient, WallError, WallUnreachable, build_server, main

KEY = "test-agent-key"
ACTOR = "test-agent"

STATE_DOC = {
    "revision": 7, "today": "2026-11-02", "now": "2026-11-02T08:00:00",
    "household": {"name": "Sample Household", "timezone": "UTC"},
    "members": [{"id": "kid_a", "name": "Kid A", "role": "kid"}, {"id": "parent_a", "role": "parent"}],
    "kids": [{"id": "kid_a", "stars": 3, "available": 3}],
    "meals": {"2026-11-02": {"title": "Soup"}}, "meal_ideas": ["Tacos"],
    "groceries": [{"id": "g1", "name": "milk", "checked": False}],
    "notes": {"text": "hello"},
    "rewards": [{"id": "r1", "title": "Movie night", "stars": 10}],
    "pending": [{"id": "req1", "member": "kid_a", "reward": "r1", "status": "pending"}],
    "decided": [],
    "calendar": {"events": [{"title": "Dentist"}], "synced_at": None},
    "weather": {}, "news": {}, "photos": [],
    "settings": {"stars": {"max_award": 5}},
    "status": {"pin_set": True},
}


class FakeWall:
    """Minimal stand-in for the wall's REST API, recording what the MCP server sends."""

    def __init__(self):
        self.requests = []
        self.state_doc = json.loads(json.dumps(STATE_DOC))
        self.do_result = {"done": True}
        self.do_error = None  # (status, message) to refuse every POST /api/do
        wall = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep pytest output clean
                pass

            def _record(self, body):
                wall.requests.append({"method": self.command, "path": self.path,
                                      "headers": {k.lower(): v for k, v in self.headers.items()},
                                      "body": body})

            def _send(self, status, payload):
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._record(None)
                if self.path == "/api/state":
                    return self._send(200, wall.state_doc)
                self._send(404, {"ok": False, "error": "Not found."})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                self._record(body)
                if self.path != "/api/do":
                    return self._send(404, {"ok": False, "error": "Not found."})
                if wall.do_error:
                    status, message = wall.do_error
                    return self._send(status, {"ok": False, "error": message})
                self._send(200, {"ok": True, "result": wall.do_result, "revision": 8})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]


@pytest.fixture
def wall():
    fake = FakeWall()
    yield fake
    fake.close()


@pytest.fixture
def server(wall):
    return build_server(wall_url=wall.url, key=KEY, actor=ACTOR)


def call(server, tool, **args) -> str:
    content, _structured = asyncio.run(server.call_tool(tool, args))
    return content[0].text


def parse(text: str):
    """Split a tool reply into (result JSON, trailing summary line)."""
    body, _, summary = text.rpartition("\n")
    return json.loads(body), summary


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ── registration, startup and transport ─────────────────────────────────────

def test_build_server_registers_eight_tools(server):
    names = sorted(tool.name for tool in asyncio.run(server.list_tools()))
    assert len(names) == 8
    assert names == sorted(TOOL_NAMES)


def test_main_refuses_blank_key(monkeypatch, capsys):
    monkeypatch.setenv("PCWALL_KEY", "   ")
    assert main([]) == 2
    assert "PCWALL_KEY" in capsys.readouterr().err
    monkeypatch.delenv("PCWALL_KEY")
    assert main([]) == 2


def test_http_transport_binds_loopback_by_default(monkeypatch):
    monkeypatch.delenv("PCWALL_MCP_HOST", raising=False)
    monkeypatch.delenv("PCWALL_MCP_PORT", raising=False)
    seen = {}

    def fake_run(self, transport="stdio", mount_path=None):
        seen.update(host=self.settings.host, port=self.settings.port, transport=transport)

    monkeypatch.setattr(mcp_server.FastMCP, "run", fake_run)
    assert main(["--key", KEY, "--transport", "sse"]) == 0
    assert seen == {"host": "127.0.0.1", "port": 8771, "transport": "sse"}

    monkeypatch.setenv("PCWALL_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("PCWALL_MCP_PORT", "9000")
    monkeypatch.setenv("PCWALL_MCP_TRANSPORT", "streamable-http")
    assert main(["--key", KEY]) == 0
    assert seen == {"host": "0.0.0.0", "port": 9000, "transport": "streamable-http"}


def test_main_defaults_to_stdio(monkeypatch):
    monkeypatch.delenv("PCWALL_MCP_TRANSPORT", raising=False)
    seen = {}
    monkeypatch.setattr(mcp_server.FastMCP, "run",
                        lambda self, transport="stdio", mount_path=None: seen.update(transport=transport))
    assert main(["--key", KEY]) == 0
    assert seen["transport"] == "stdio"


def test_unreachable_wall_becomes_tool_error_with_url():
    url = f"http://127.0.0.1:{free_port()}"
    client = WallClient(url, KEY, ACTOR, timeout=2)
    with pytest.raises(WallUnreachable) as raised:
        client.state()
    assert raised.value.url == f"{url}/api/state"
    server = build_server(client=client)
    with pytest.raises(ToolError) as tool_error:
        call(server, "wall_note", text="hi")
    assert f"{url}/api/do" in str(tool_error.value)


# ── tool mapping against the fake wall ──────────────────────────────────────

def test_every_call_carries_key_and_actor_headers(server, wall):
    call(server, "wall_state")
    call(server, "wall_note", text="hello wall")
    assert len(wall.requests) == 2
    for request in wall.requests:
        assert request["headers"]["x-wall-key"] == KEY
        assert request["headers"]["x-wall-actor"] == ACTOR
    assert wall.requests[1]["headers"]["content-type"] == "application/json"


def test_actor_defaults_to_agent(wall):
    server = build_server(wall_url=wall.url, key=KEY)
    call(server, "wall_state")
    assert wall.requests[0]["headers"]["x-wall-actor"] == "agent"


def test_wall_complete_step_maps_complete_and_uncomplete(server, wall):
    call(server, "wall_complete_step", member="kid_a", routine="morning", step="teeth")
    call(server, "wall_complete_step", member="kid_a", routine="morning", step="teeth", undo=True)
    assert [p["body"] for p in wall.posts] == [
        {"op": "complete", "args": {"member": "kid_a", "item": "step:morning:teeth"}},
        {"op": "uncomplete", "args": {"member": "kid_a", "item": "step:morning:teeth"}},
    ]


def test_wall_award_stars_maps_award_stars(server, wall):
    text = call(server, "wall_award_stars", member="kid_a", stars=3, reason="helped with dishes")
    assert wall.posts[-1]["body"] == {"op": "award_stars",
                                      "args": {"who": "kid_a", "amount": 3, "reason": "helped with dishes"}}
    assert "3 star" in parse(text)[1]


def test_award_stars_bounds_from_settings(server, wall):
    with pytest.raises(ToolError) as too_many:
        call(server, "wall_award_stars", member="kid_a", stars=6, reason="x")
    assert "between 1 and 5" in str(too_many.value)
    with pytest.raises(ToolError):
        call(server, "wall_award_stars", member="kid_a", stars=0, reason="x")
    assert wall.posts == []  # nothing was sent to the wall
    call(server, "wall_award_stars", member="kid_a", stars=5, reason="x")
    assert wall.posts[-1]["body"]["args"]["amount"] == 5

    wall.state_doc.pop("settings")  # no ceiling published: fall back to 1..100
    wall.requests.clear()
    call(server, "wall_award_stars", member="kid_a", stars=100, reason="big day")
    assert wall.posts[-1]["body"]["args"]["amount"] == 100
    with pytest.raises(ToolError) as over:
        call(server, "wall_award_stars", member="kid_a", stars=101, reason="x")
    assert "between 1 and 100" in str(over.value)


def test_wall_pending_rewards_reads_rewards_op(server, wall):
    wall.do_result = {"rewards": [], "pending": [{"id": "req1", "member": "kid_a"}]}
    result, summary = parse(call(server, "wall_pending_rewards"))
    assert wall.posts[-1]["body"] == {"op": "rewards", "args": {}}
    assert result == {"pending": [{"id": "req1", "member": "kid_a"}]}
    assert summary == "1 pending reward request(s)."


def test_wall_decide_reward_maps_approve_and_decline(server, wall):
    call(server, "wall_decide_reward", request_id="req1", approve=True)
    call(server, "wall_decide_reward", request_id="req1", approve=False)
    assert [p["body"] for p in wall.posts] == [
        {"op": "approve_reward", "args": {"id": "req1"}},
        {"op": "decline_reward", "args": {"id": "req1"}},
    ]


def test_wall_set_dinner_maps_set_meal(server, wall):
    call(server, "wall_set_dinner", meal="Tacos")
    call(server, "wall_set_dinner", meal="Soup", date="2026-11-03")
    assert [p["body"] for p in wall.posts] == [
        {"op": "set_meal", "args": {"title": "Tacos"}},
        {"op": "set_meal", "args": {"title": "Soup", "day": "2026-11-03"}},
    ]


def test_wall_groceries_maps_four_actions(server, wall):
    wall.do_result = {"items": [{"name": "milk"}]}
    call(server, "wall_groceries", action="list")
    call(server, "wall_groceries", action="add", item="eggs")
    call(server, "wall_groceries", action="check", item="eggs")
    call(server, "wall_groceries", action="remove", item="eggs")
    assert [p["body"] for p in wall.posts] == [
        {"op": "groceries", "args": {}},
        {"op": "grocery_add", "args": {"items": ["eggs"]}},
        {"op": "grocery_check", "args": {"items": ["eggs"], "checked": True}},
        {"op": "grocery_remove", "args": {"items": ["eggs"]}},
    ]
    with pytest.raises(ToolError):
        call(server, "wall_groceries", action="add")  # item required
    with pytest.raises(ToolError):
        call(server, "wall_groceries", action="eat", item="eggs")
    assert len(wall.posts) == 4


def test_wall_note_maps_note_set(server, wall):
    call(server, "wall_note", text="Grandparents visit Sunday")
    assert wall.posts[-1]["body"] == {"op": "note_set", "args": {"text": "Grandparents visit Sunday"}}


def test_wall_state_section_filter(server, wall):
    full, _ = parse(call(server, "wall_state"))
    assert full["revision"] == 7 and "kids" in full and "settings" in full
    groceries, summary = parse(call(server, "wall_state", section="groceries"))
    assert groceries == {"groceries": STATE_DOC["groceries"]}
    assert "groceries" in summary
    meals, _ = parse(call(server, "wall_state", section="meals"))
    assert set(meals) == {"meals", "meal_ideas"}
    stars, _ = parse(call(server, "wall_state", section="stars"))
    assert set(stars) == {"kids", "rewards", "pending", "decided"}
    assert set(parse(call(server, "wall_state", section="calendar"))[0]) == {"calendar"}
    assert "household" in parse(call(server, "wall_state", section="home"))[0]
    with pytest.raises(ToolError) as bad:
        call(server, "wall_state", section="photos")
    assert "Unknown section" in str(bad.value)
    assert all(r["method"] == "GET" and r["path"] == "/api/state" for r in wall.requests)


def test_error_code_surfaces_in_tool_error(server, wall):
    wall.do_error = (403, "Ask a parent to do that.")
    with pytest.raises(ToolError) as refused:
        call(server, "wall_note", text="x")
    assert "403" in str(refused.value)
    assert "Ask a parent to do that." in str(refused.value)
    client = WallClient(wall.url, KEY, ACTOR)
    with pytest.raises(WallError) as raised:
        client.do("note_set", {"text": "x"})
    assert (raised.value.status, raised.value.message, raised.value.op) == (403, "Ask a parent to do that.", "note_set")


def test_summary_line_appended(server, wall):
    wall.do_result = {"text": "Dinner at six", "by": ACTOR}
    text = call(server, "wall_note", text="Dinner at six")
    result, summary = parse(text)
    assert result == {"text": "Dinner at six", "by": ACTOR}
    assert summary == "Family note set: Dinner at six"
    assert "\n" not in summary
    _, dinner = parse(call(server, "wall_set_dinner", meal="Soup", date="2026-11-03"))
    assert dinner == "Dinner on 2026-11-03: Soup."


# ── architecture invariant ──────────────────────────────────────────────────

def test_no_store_or_engine_imports():
    source = pathlib.Path(mcp_server.__file__).read_text(encoding="utf-8")
    forbidden = {"pcwall.store", "pcwall.engine", "pcwall.model", "pcwall.server", "pcwall.access"}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden
                assert not alias.name.startswith("pcwall.")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module not in forbidden
            assert not (module == "pcwall" or module.startswith("pcwall."))
            assert node.level == 0, "no relative imports into the pcwall package"
    assert "urllib.request" in source
    assert os.path.basename(mcp_server.__file__) == "mcp_server.py"

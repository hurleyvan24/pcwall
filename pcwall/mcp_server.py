"""pcwall MCP server: eight FastMCP tools that let an agent read and change the family wall.

The server is a thin HTTP client. It never opens the wall's data document, never imports
pcwall.store or pcwall.engine, and never writes to disk: every tool is one GET /api/state
or one POST /api/do against the running wall, carrying the agent key in X-Wall-Key and the
actor name in X-Wall-Actor, exactly as a phone or any other client would. The wall stays
the single authority on roles, validation and the audit ledger. See docs/mcp.md.

Run it with ``python -m pcwall.mcp_server``; PCWALL_KEY must be set. Transport is stdio
unless PCWALL_MCP_TRANSPORT says otherwise, and the HTTP transports bind 127.0.0.1 unless
PCWALL_MCP_HOST is set.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from typing import Optional

from mcp.server.fastmcp import FastMCP

DEFAULT_WALL_URL = "http://127.0.0.1:8770"
DEFAULT_ACTOR = "agent"
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8771
DEFAULT_TRANSPORT = "stdio"
TRANSPORTS = ("stdio", "sse", "streamable-http")
DEFAULT_MAX_AWARD = 100
REQUEST_TIMEOUT = 10.0

# Which top-level keys of the wall's state document each named section keeps.
SECTIONS = {
    "home": ("household", "members", "today", "now", "notes", "weather", "status"),
    "calendar": ("calendar",),
    "meals": ("meals", "meal_ideas"),
    "groceries": ("groceries",),
    "stars": ("kids", "rewards", "pending", "decided"),
}
GROCERY_ACTIONS = ("list", "add", "check", "remove")

TOOL_NAMES = ("wall_state", "wall_complete_step", "wall_award_stars", "wall_pending_rewards",
              "wall_decide_reward", "wall_set_dinner", "wall_groceries", "wall_note")


class WallUnreachable(Exception):
    """The wall did not answer at all (refused connection, DNS failure, timeout)."""

    def __init__(self, url: str, cause: Optional[BaseException] = None):
        self.url = url
        self.cause = cause
        detail = f" ({cause})" if cause is not None else ""
        super().__init__(f"Wall unreachable at {url}{detail}. Is pcwall running, and is PCWALL_URL right?")


class WallError(Exception):
    """The wall answered and refused: an HTTP status plus the wall's own error message."""

    def __init__(self, status: int, message: str, op: Optional[str] = None):
        self.status = status
        self.message = message
        self.op = op
        where = f" for op {op!r}" if op else ""
        super().__init__(f"Wall refused{where}: HTTP {status}: {message}")


class WallClient:
    """Speaks the wall's REST API over urllib. Every request carries X-Wall-Key and X-Wall-Actor."""

    def __init__(self, wall_url: str, key: str, actor: str = DEFAULT_ACTOR, timeout: float = REQUEST_TIMEOUT):
        self.wall_url = (wall_url or DEFAULT_WALL_URL).rstrip("/")
        self.key = key or ""
        self.actor = (actor or DEFAULT_ACTOR).strip() or DEFAULT_ACTOR
        self.timeout = timeout

    def headers(self) -> dict:
        return {"X-Wall-Key": self.key, "X-Wall-Actor": self.actor, "Accept": "application/json"}

    def _request(self, method: str, path: str, body: Optional[dict] = None, op: Optional[str] = None) -> dict:
        url = f"{self.wall_url}{path}"
        data = None
        headers = self.headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            raise WallUnreachable(url, exc) from exc
        payload = _decode(raw)
        if status >= 400 or (isinstance(payload, dict) and payload.get("ok") is False):
            message = payload.get("error") if isinstance(payload, dict) else None
            raise WallError(status, str(message or raw.decode("utf-8", "replace")[:200] or "no detail"), op)
        if not isinstance(payload, dict):
            raise WallError(status, "The wall sent something that was not a JSON object.", op)
        return payload

    def state(self, section: Optional[str] = None) -> dict:
        """GET /api/state, trimmed to one of SECTIONS when a section is named."""
        if section is not None:
            section = section.strip().lower()
            if section not in SECTIONS:
                raise ValueError(f"Unknown section {section!r}; choose one of {', '.join(SECTIONS)}.")
        document = self._request("GET", "/api/state")
        if section is None:
            return document
        return {key: document[key] for key in SECTIONS[section] if key in document}

    def do(self, op: str, args: Optional[dict] = None) -> dict:
        """POST /api/do and return the op's result (the "result" field of the wall's reply)."""
        payload = self._request("POST", "/api/do", {"op": op, "args": dict(args or {})}, op=op)
        return payload.get("result", payload)


def _decode(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return None


def _reply(result, summary: str) -> str:
    """Every tool answers with the op result as JSON and one human-readable line under it."""
    return json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n" + summary.strip()


def _require(value: Optional[str], name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{name}' is required.")
    return value.strip()


def _max_award(client: WallClient) -> int:
    """The parent's award ceiling from settings.stars.max_award; DEFAULT_MAX_AWARD when absent."""
    try:
        settings = client.state().get("settings") or {}
        value = (settings.get("stars") or {}).get("max_award")
    except (WallUnreachable, WallError, AttributeError):
        return DEFAULT_MAX_AWARD
    return value if type(value) is int and value >= 1 else DEFAULT_MAX_AWARD


def build_server(wall_url: Optional[str] = None, key: Optional[str] = None, actor: Optional[str] = None,
                 host: Optional[str] = None, port: Optional[int] = None,
                 client: Optional[WallClient] = None) -> FastMCP:
    """Register the eight wall tools on a FastMCP server and return it.

    Arguments fall back to PCWALL_URL, PCWALL_KEY, PCWALL_MCP_ACTOR, PCWALL_MCP_HOST and
    PCWALL_MCP_PORT. A prebuilt client (tests) replaces url/key/actor.
    """
    wall_url = wall_url or os.environ.get("PCWALL_URL") or DEFAULT_WALL_URL
    key = key if key is not None else os.environ.get("PCWALL_KEY", "")
    actor = actor or os.environ.get("PCWALL_MCP_ACTOR") or DEFAULT_ACTOR
    host = host or os.environ.get("PCWALL_MCP_HOST") or DEFAULT_MCP_HOST
    port = port if port is not None else int(os.environ.get("PCWALL_MCP_PORT") or DEFAULT_MCP_PORT)
    wall = client or WallClient(wall_url, key, actor)

    server = FastMCP(
        "pcwall",
        instructions="Tools for the pcwall family display. Read with wall_state; every change goes "
                     "through the wall's own API and is recorded under the configured actor name.",
        host=host, port=port)
    server.wall = wall  # type: ignore[attr-defined]

    @server.tool()
    def wall_state(section: Optional[str] = None) -> str:
        """Read the wall. Optional section: home, calendar, meals, groceries or stars."""
        document = wall.state(section)
        if section:
            summary = f"Section '{section.strip().lower()}' of the wall state ({len(document)} keys)."
        else:
            kids = document.get("kids") or []
            summary = (f"Wall state at revision {document.get('revision')}: {len(kids)} kid(s), "
                       f"{len(document.get('groceries') or [])} grocery item(s), "
                       f"{len(document.get('pending') or [])} pending reward request(s).")
        return _reply(document, summary)

    @server.tool()
    def wall_complete_step(member: str, routine: str, step: str, undo: bool = False) -> str:
        """Mark one routine step done for a kid today (undo=True unmarks it). Ids, not names."""
        member, routine, step = _require(member, "member"), _require(routine, "routine"), _require(step, "step")
        op = "uncomplete" if undo else "complete"
        result = wall.do(op, {"member": member, "item": f"step:{routine}:{step}"})
        verb = "Unmarked" if undo else "Marked"
        return _reply(result, f"{verb} step '{step}' of routine '{routine}' for {member}.")

    @server.tool()
    def wall_award_stars(member: str, stars: int, reason: str) -> str:
        """Give a kid bonus stars with a reason. Stars must be 1..max_award (wall setting)."""
        member, reason = _require(member, "member"), _require(reason, "reason")
        if type(stars) is not int:
            raise ValueError("'stars' must be a whole number.")
        ceiling = _max_award(wall)
        if not 1 <= stars <= ceiling:
            raise ValueError(f"'stars' must be between 1 and {ceiling} (the wall's max_award).")
        result = wall.do("award_stars", {"who": member, "amount": stars, "reason": reason})
        return _reply(result, f"Awarded {stars} star(s) to {member}: {reason}")

    @server.tool()
    def wall_pending_rewards() -> str:
        """List reward requests waiting for a parent's decision."""
        pending = (wall.do("rewards") or {}).get("pending") or []
        return _reply({"pending": pending}, f"{len(pending)} pending reward request(s).")

    @server.tool()
    def wall_decide_reward(request_id: str, approve: bool) -> str:
        """Approve (True) or decline (False) a pending reward request by its id."""
        request_id = _require(request_id, "request_id")
        if type(approve) is not bool:
            raise ValueError("'approve' must be true or false.")
        op = "approve_reward" if approve else "decline_reward"
        result = wall.do(op, {"id": request_id})
        return _reply(result, f"{'Approved' if approve else 'Declined'} reward request {request_id}.")

    @server.tool()
    def wall_set_dinner(meal: str, date: Optional[str] = None) -> str:
        """Plan dinner for today or a YYYY-MM-DD date."""
        meal = _require(meal, "meal")
        args = {"title": meal}
        if date:
            args["day"] = date.strip()
        result = wall.do("set_meal", args)
        return _reply(result, f"Dinner on {date.strip() if date else 'today'}: {meal}.")

    @server.tool()
    def wall_groceries(action: str, item: Optional[str] = None) -> str:
        """Grocery list: action is list, add, check or remove; item names the grocery for the last three."""
        action = _require(action, "action").lower()
        if action not in GROCERY_ACTIONS:
            raise ValueError(f"'action' must be one of {', '.join(GROCERY_ACTIONS)}.")
        if action == "list":
            result = wall.do("groceries")
            items = result.get("items") if isinstance(result, dict) else None
            return _reply(result, f"{len(items or [])} item(s) on the grocery list.")
        item = _require(item, "item")
        if action == "add":
            result = wall.do("grocery_add", {"items": [item]})
            return _reply(result, f"Added '{item}' to the grocery list.")
        if action == "check":
            result = wall.do("grocery_check", {"items": [item], "checked": True})
            return _reply(result, f"Checked off '{item}'.")
        result = wall.do("grocery_remove", {"items": [item]})
        return _reply(result, f"Removed '{item}' from the grocery list.")

    @server.tool()
    def wall_note(text: str) -> str:
        """Replace the family note shown on the wall."""
        text = _require(text, "text")
        result = wall.do("note_set", {"text": text})
        preview = text if len(text) <= 60 else text[:57] + "..."
        return _reply(result, f"Family note set: {preview}")

    return server


def _parse(argv: Optional[list]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m pcwall.mcp_server",
                                     description="MCP server for the pcwall family display.")
    parser.add_argument("--url", default=None, help="wall base URL (PCWALL_URL)")
    parser.add_argument("--key", default=None, help="agent key (PCWALL_KEY)")
    parser.add_argument("--actor", default=None, help="X-Wall-Actor name (PCWALL_MCP_ACTOR)")
    parser.add_argument("--transport", default=None, choices=TRANSPORTS, help="stdio (default), sse or streamable-http")
    parser.add_argument("--host", default=None, help="bind address for HTTP transports (PCWALL_MCP_HOST)")
    parser.add_argument("--port", default=None, type=int, help="port for HTTP transports (PCWALL_MCP_PORT)")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    """Entry point. Exit 2 when no agent key is configured or the transport is unknown."""
    args = _parse(list(sys.argv[1:] if argv is None else argv))
    key = (args.key if args.key is not None else os.environ.get("PCWALL_KEY", "")).strip()
    if not key:
        print("pcwall.mcp_server: PCWALL_KEY is not set. Set it to the wall's agent key "
              "(or pass --key) and start again.", file=sys.stderr)
        return 2
    transport = (args.transport or os.environ.get("PCWALL_MCP_TRANSPORT") or DEFAULT_TRANSPORT).strip().lower()
    if transport not in TRANSPORTS:
        print(f"pcwall.mcp_server: unknown transport {transport!r}; use one of {', '.join(TRANSPORTS)}.",
              file=sys.stderr)
        return 2
    try:
        server = build_server(wall_url=args.url, key=key, actor=args.actor, host=args.host, port=args.port)
    except ValueError as exc:
        print(f"pcwall.mcp_server: {exc}", file=sys.stderr)
        return 2
    server.run(transport=transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())

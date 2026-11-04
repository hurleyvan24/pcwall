# pcwall MCP server

`pcwall/mcp_server.py` exposes the family wall to an AI agent over the
[Model Context Protocol](https://modelcontextprotocol.io). Claude Desktop, Claude Code,
Cursor or any other MCP client can read the wall and make the same changes a parent's
phone can: tick a routine step, award stars, decide a reward request, plan dinner, keep
the grocery list and write the family note.

## Architecture

The MCP server is a pure HTTP client of the running wall. It owns no data.

```
MCP client (Claude Desktop, ...)
        │  stdio / SSE / streamable-http
        ▼
python -m pcwall.mcp_server          (FastMCP, 8 tools)
        │  GET  /api/state
        │  POST /api/do  {"op": ..., "args": {...}}
        │  headers: X-Wall-Key, X-Wall-Actor
        ▼
pcwall server (uvicorn, port 8770)   → store, engine, ledger
```

- Every tool is exactly one `GET /api/state` or one `POST /api/do` against the wall's REST
  API, built with the standard library's `urllib.request`. There is no second code path:
  the module never imports `pcwall.store` or `pcwall.engine`, never opens the data
  document and never writes to disk. `tests/test_mcp.py` checks that with an AST walk.
- The wall stays the single authority. Roles, validation, the star ledger and the audit
  trail all happen inside the wall, so an agent cannot do anything a phone with the same
  key could not do.
- Every request carries two headers: `X-Wall-Key` (the agent key the wall was started
  with, see `PCWALL_KEY` in the wall's own configuration) and `X-Wall-Actor` (the name
  the wall records as "by" on every change, default `agent`).
- Every tool answers with the op's result as pretty-printed JSON followed by one
  human-readable summary line, so a model can quote either.
- When the wall cannot be reached at all (not running, wrong URL, timeout) the tool fails
  with a `WallUnreachable` error that names the exact URL it tried, for example
  `Wall unreachable at http://127.0.0.1:8770/api/state (...)`. When the wall answers and
  refuses, the tool error carries the HTTP status and the wall's own message, for example
  `Wall refused for op 'award_stars': HTTP 403: Ask a parent to do that.`

## Configuration

All settings come from the environment; the same values can be given as command-line
flags (`--url`, `--key`, `--actor`, `--transport`, `--host`, `--port`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `PCWALL_URL` | `http://127.0.0.1:8770` | Base URL of the running wall. |
| `PCWALL_KEY` | (none, required) | Agent key sent as `X-Wall-Key`. The server refuses to start (exit code 2) when it is blank or unset. |
| `PCWALL_MCP_ACTOR` | `agent` | Name sent as `X-Wall-Actor`; the wall records it as who made each change. |
| `PCWALL_MCP_TRANSPORT` | `stdio` | `stdio` for desktop clients, `sse` or `streamable-http` to serve over HTTP. |
| `PCWALL_MCP_HOST` | `127.0.0.1` | Bind address for the HTTP transports. Loopback unless you set it; set `0.0.0.0` only behind a firewall you trust. |
| `PCWALL_MCP_PORT` | `8771` | Port for the HTTP transports. |

Start it by hand to check the key and URL:

```
set PCWALL_KEY=<the wall's agent key>          # Windows
export PCWALL_KEY=<the wall's agent key>       # macOS / Linux
python -m pcwall.mcp_server
```

With a blank key it prints a one-line reason to stderr and exits with code 2. To serve
over HTTP instead of stdio:

```
PCWALL_MCP_TRANSPORT=streamable-http PCWALL_MCP_PORT=8771 python -m pcwall.mcp_server
```

## Claude Desktop

Add the server to `claude_desktop_config.json` (Settings → Developer → Edit Config).
Use the Python that has `pcwall` and the `mcp` package installed via
`python -m pip install -e ".[mcp]"`, and set `cwd` to the checkout if `pcwall` is not
installed as a package. The server needs the 1.x SDK because mcp 2.x renamed `FastMCP`.

```json
{
  "mcpServers": {
    "pcwall": {
      "command": "python",
      "args": ["-m", "pcwall.mcp_server"],
      "cwd": "/path/to/pcwall",
      "env": {
        "PCWALL_URL": "http://127.0.0.1:8770",
        "PCWALL_KEY": "<the wall's agent key>",
        "PCWALL_MCP_ACTOR": "claude"
      }
    }
  }
}
```

Claude Code takes the same shape through `claude mcp add pcwall -e PCWALL_KEY=... -- python -m pcwall.mcp_server`.

## Tool reference

All ids are the lowercase ids from `config/family.json` (members, routines, steps,
rewards), not display names. Call `wall_state` first when unsure of an id.

| Tool | Parameters | What it does | Wall op | Example prompt |
| --- | --- | --- | --- | --- |
| `wall_state` | `section` (optional: `home`, `calendar`, `meals`, `groceries`, `stars`) | Reads the wall's state document, trimmed to one section when asked. Summary line counts kids, groceries and pending requests. | `GET /api/state` | "What is on the wall right now?" / "Show me this week's meals." |
| `wall_complete_step` | `member`, `routine`, `step`, `undo` (default `false`) | Marks one routine step done for a kid today; `undo=true` unmarks it. | `complete` / `uncomplete` with `item = step:<routine>:<step>` | "Tick teeth in the morning routine for kid_a." |
| `wall_award_stars` | `member`, `stars` (1..`max_award`), `reason` | Gives a kid bonus stars with a reason. The ceiling comes from the wall's `settings.stars.max_award`; when the wall does not publish one, 1..100 applies. Out-of-range values are refused before anything is sent. | `award_stars` | "Give kid_a 2 stars for helping with the dishes." |
| `wall_pending_rewards` | none | Lists reward requests waiting for a parent's decision. | `rewards` (the `pending` part) | "Any reward requests waiting?" |
| `wall_decide_reward` | `request_id`, `approve` (`true`/`false`) | Approves or declines a pending reward request. | `approve_reward` / `decline_reward` | "Approve the movie night request." |
| `wall_set_dinner` | `meal`, `date` (optional `YYYY-MM-DD`, default today) | Plans dinner for a day. | `set_meal` | "Dinner tomorrow is tacos." |
| `wall_groceries` | `action` (`list`, `add`, `check`, `remove`), `item` (required except for `list`) | Reads or edits the grocery list one item at a time. | `groceries` / `grocery_add` / `grocery_check` / `grocery_remove` | "Add eggs to the groceries." / "Check off milk." |
| `wall_note` | `text` | Replaces the family note shown on the wall. | `note_set` | "Put 'Grandparents visit Sunday' on the wall." |

Every tool's answer looks like this: the op's JSON result, then one summary line.

```
{
  "text": "Grandparents visit Sunday",
  "by": "claude"
}
Family note set: Grandparents visit Sunday
```

### Errors an agent will see

| Situation | Tool error contains |
| --- | --- |
| Wall not running / wrong `PCWALL_URL` | `Wall unreachable at <url tried>` |
| Wrong or revoked key | `HTTP 401` or `HTTP 403` and the wall's message |
| Op refused by the wall (unknown id, role, validation) | `HTTP 400` and the wall's message, e.g. `Missing 'member'.` |
| Stars outside `1..max_award` | `'stars' must be between 1 and <max_award>` (nothing was sent) |
| Unknown `section` or grocery `action` | the accepted values |

## Security notes

- The key is read from the environment or `--key` and is never printed or logged.
- The HTTP transports bind `127.0.0.1` by default. Exposing the MCP server on another
  interface exposes the wall's agent key to whoever can reach that port; keep it on the
  local machine or behind a trusted network.
- The wall records `X-Wall-Actor` on every change, so set `PCWALL_MCP_ACTOR` to something
  that tells the family which assistant acted.

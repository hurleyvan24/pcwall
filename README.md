# pcwall

pcwall is a self-hosted family wall display. A small Python service runs on a computer
at home and serves one page to a tablet or TV in the kitchen: the kids' routines and
stars, the rewards they are saving for, today's calendar, family photos, the weather,
headlines, dinner, the grocery list and a note from a parent. A paired phone gets the
parent view behind a PIN, and an AI assistant can talk to the wall over MCP.

- **No cloud account required.** The wall document lives in `data/` on your machine.
  Google Calendar and Photos are optional, read-only, and sign in with your own OAuth
  client.
- **No build step.** Python 3.11 or newer, [Starlette](https://www.starlette.io/) on
  uvicorn, and a static front end served from `pcwall/static/`.
- **One screen, no scrolling.** The layout scales as a whole to fit 1080p monitors,
  15.6 inch tablets, small HDMI panels and TVs.
- **Three roles.** The wall itself (kiosk), a parent's phone (parent) and automation
  holding a shared key (agent). The server enforces all three.

Contents: [Quickstart](#quickstart) · [Configuration](#configuration) ·
[Google Calendar and Photos](#google-calendar-and-photos-optional) ·
[Customising with an AI agent](#customising-the-wall-with-an-ai-agent) ·
[Display scaling and tablets](#display-scaling-and-tablets) ·
[Running as a service](#running-as-a-service) · [MCP](#talking-to-the-wall-from-an-ai-assistant-mcp) ·
[Development](#development) · [Security](#security)

## Quickstart

```sh
git clone https://github.com/pcwall/pcwall.git
cd pcwall
python -m venv .venv
.venv\Scripts\activate            # Windows
source .venv/bin/activate         # macOS / Linux
python -m pip install -e ".[dev,google,mcp]"

cp config/family.example.json config/family.json      # macOS / Linux
copy config\family.example.json config\family.json    # Windows

python -m pcwall.config --check-config
python -m pcwall
```

The server listens on port `8770` by default and prints three lines when it is ready:

```text
pcwall is ready at http://<lan-ip>:8770/
Pair the kitchen screen with code 123456 (kiosk, valid 10 minutes).
Pair a parent's phone with code 654321 (parent, valid 10 minutes).
```

Open the URL on the tablet and type the kiosk code on its keypad; open it on your phone
and type the parent code. Codes work once and expire after ten minutes; restart the
server to get fresh ones. Each paired device keeps a cookie for over a year, so pairing
is a one-off per device.

`--check-config` validates `config/family.json` and prints the members, routines and
rewards as a table without starting the server (the PIN is never printed). When the
file is invalid, it names the exact JSON path and the rule it breaks. The same check
runs on startup, and a missing `config/family.json` is created from the sample
automatically with a one-line notice.

## Configuration

Everything that describes your household lives in two gitignored files: the family
file and `.env`. Nothing in the repository needs to be edited.

### The family file

`config/family.json` holds the household name and time zone, the school year settings,
the members (kids with birthdates, parents, a shared "family" member), the routines with
their steps and star values, per-kid overrides such as a later bedtime, the rewards and
their star prices, and the parent PIN. Start from `config/family.example.json`, which
ships with a complete sample household, and check your edits with
`python -m pcwall.config --check-config`.

The schema rules are listed in [`AGENTS.md`](AGENTS.md), which is written so an AI coding
agent can fill the file in for you (see [Customising the wall with an AI
agent](#customising-the-wall-with-an-ai-agent)). The short version:

- `household.timezone` is an IANA zone and drives every clock, timer and daily rollover.
- Every `id` is lowercase ASCII and unique in its list. A `color` is one of `violet`,
  `blue`, `green`, `orange`, `slate` or `stone`.
- Kids have a `birthdate`, which sets the school grade shown on the wall. Twins name
  each other with `twin`.
- A routine has `days`, `start`, `end` and `steps`; a step is worth 1 star unless it says
  otherwise. `overrides` changes a routine for one kid only.
- `parent_pin` is 4 to 8 digits. It is hashed with PBKDF2-SHA256 into the wall document on
  the first run; after that the plaintext in the file is ignored, and the wall shows a
  reminder until the sample `1234` has been changed.

### Environment

The server reads its settings from its process environment. Copy `.env.example` to
`.env` and fill in what you need. The [service installers](#running-as-a-service) feed
`.env` to the service; for a manual run, export the variables in your shell first.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PCWALL_HOST` | `0.0.0.0` | Interface the service binds to. |
| `PCWALL_PORT` | `8770` | Port the service listens on. |
| `PCWALL_PUBLIC_URL` | unset | URL printed in the startup banner when the wall sits behind a reverse proxy or a domain name. |
| `PCWALL_KEY` | unset | Shared secret for the agent role. Unset reads `data/pcwall_key.txt`; blank disables agent calls entirely. |
| `PCWALL_CONFIG_DIR` | `config/` | Directory holding `family.json`, for a service that keeps its configuration elsewhere. |
| `PCWALL_LAT`, `PCWALL_LON` | unset | Coordinates for the weather panel; without them the panel stays empty. Values saved in the wall's weather settings take precedence. |
| `PCWALL_CITY` | `Springfield` | Label shown next to the weather. |
| `PCWALL_WEATHER_UNITS` | `fahrenheit` | `fahrenheit` or `celsius`. |
| `GOOGLE_CREDENTIALS_FILE` | `credentials.json` | OAuth client downloaded from Google Cloud Console. |
| `GOOGLE_TOKEN_FILE` | `data/google_token.json` | Where this wall caches its Google token after sign-in. |
| `PCWALL_DRESS_QUERY` | `subject:dress OR subject:spirit` | Gmail search for the optional school dress-day scanner. |

Relative paths are resolved against the repository root, never the current directory.
Generate a key with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

### Roles, pairing and the parent PIN

| Role | Who | How it is granted |
| --- | --- | --- |
| `kiosk` | The wall itself | A kiosk pairing code typed on the screen. Kids tick their own steps and ask for rewards. |
| `parent` | A parent's phone | A parent pairing code. Everything, with star awards and reward decisions behind the parent PIN. |
| `agent` | Automation and AI assistants | The shared key in `PCWALL_KEY`, sent as `X-Wall-Key`, acting for the parent named in `X-Wall-Actor`. |

Pairing codes are printed at startup, and a paired parent can request new ones through
the API's `pairing_code` action without restarting the server. Wrong codes and wrong
PINs are rate-limited per client address. Star changes are written to a ledger with the
role and person that made them.

## Google Calendar and Photos (optional)

Google is optional: the wall works without signing in. Connecting grants read-only
Calendar access and permission to pick photos; the Photos Picker shares only the items
you select, never your whole library. The full walkthrough with troubleshooting is in
[`docs/google-setup.md`](docs/google-setup.md).

```sh
python -m pip install google-api-python-client google-auth-oauthlib
```

### Path A: your own Google Cloud project

Allow about three minutes in the Google Cloud Console, plus your first sign-in.

1. Create or select a project and enable the **Google Calendar API** and the **Photos
   Picker API**.
2. Configure the OAuth consent screen: external audience, publishing status **Testing**,
   and add the Google account that owns the family calendar as a test user.
3. Add the scopes `calendar.readonly` and `photospicker.mediaitems.readonly`.
4. Create an OAuth client of type **Desktop app** and save the download as
   `credentials.json` in the repository root. It must be an OAuth client, not a
   service-account key.
5. On the computer that hosts the wall, run `python -m pcwall.google_auth`. A browser
   opens, you sign in as the test user and grant both permissions, and the command prints
   where it saved the token. It exits with status 1 if anything went wrong.
6. Start or restart the wall, then choose calendars and pick photos from the parent view.

Apps left in Testing get refresh tokens that expire after seven days; run the sign-in
again when that happens, or move the app to production in the console.

### Path B: a friend's shared OAuth client

Friends can skip creating a project of their own:

1. The project owner adds the friend's Google address as a test user on the owner's
   OAuth app.
2. The owner sends the Desktop OAuth client's `credentials.json` to the friend through a
   private channel. The client JSON identifies the shared app; it does not grant access
   to the owner's account. The owner never sends their own token file.
3. The friend places `credentials.json` in their own checkout, installs the Google
   packages, runs `python -m pcwall.google_auth` on their own wall computer and signs in
   with their own account.
4. The friend's tokens, calendar data and chosen photos stay on the friend's wall.

Both files are ignored by git, and the background connector never opens a browser: it
refreshes a valid token or leaves Google disconnected and keeps the last cached events.

## Customising the wall with an AI agent

The quickest way to describe your family is to let a coding agent edit the family file.
Paste the prompt below into Claude, Cursor, ChatGPT, Copilot or any similar tool, then
describe your household underneath it. The same prompt ships in
[`AGENTS.md`](AGENTS.md), so every agent is pointed at the same rules.

> You are customising a pcwall family display. Edit only `config/family.json`. Keep `schema` at 1. Replace the sample members with the family described below, keeping ids lowercase ASCII, one `color` per person from violet, blue, green, orange, slate, stone, and emoji from Unicode 12 or older. Keep at least one parent. Rewrite routines, steps and rewards to match the family's day; a step is worth 1 star unless told otherwise. Put a per-kid bedtime in `overrides`. Set `parent_pin` to the number the parent gives you or leave `1234`. Do not edit any Python, JavaScript or CSS. When done, run `python -m pcwall --check-config` and fix anything it reports.

Underneath the prompt, tell the agent each person's name, role (`kid`, `parent` or
`shared`), birthdate for kids, favourite colour and emoji, who the twins are if any,
what a school morning, afternoon and bedtime look like, which kids go to bed later, and
the rewards the kids can save stars for. Create `config/family.json` from the sample
before the agent starts, and run `python -m pcwall.config --check-config` afterwards
until it exits 0.

## Display scaling and tablets

The whole layout hangs off one CSS variable, `--ui-scale`, so a single number resizes
every card, font and icon together and the wall fills one screen without scrolling.
Keep the browser's own zoom at 100 % and let the wall do the scaling.

| Preset | Scale | Intended display |
| --- | --- | --- |
| `standard_1080p` | 1.00 | 1080p monitors and TVs |
| `tablet_15_6` | 0.85 | 15.6 inch 1080p Android tablets, which report a 1280 x 720 viewport |
| `hd_720p` | 0.75 | 720p panels and other small displays |
| `glanceable` | 1.15 | Reading from across the room, few kids or routines |
| `auto` | fit | Measured from the viewport on load and resize, clamped to 0.60 to 1.40 |

A display picks its scale in this order: the value stored on that device from the
Settings panel, then a `?preset=` or `?scale=` query on the URL, then the server
default. Put the right preset in the kiosk's start URL once and it sticks:

```text
http://<lan-ip>:8770/?preset=tablet_15_6
```

[`docs/display-scaling.md`](docs/display-scaling.md) explains device pixel ratios and
how to find the right preset for an unknown panel.
[`scripts/fully_kiosk.md`](scripts/fully_kiosk.md) walks through turning an Android
tablet into a locked-down wall with Fully Kiosk Browser: start URL, zoom and viewport
settings, sleep schedule, daily reload, remote admin and kiosk lock mode.

## Running as a service

Both installers register `python -m pcwall server`, start it with the machine, restart
it whenever it exits, and append its console output to `logs/pcwall.log` and
`logs/pcwall.err.log` under the repository. Both read `.env` from the repository root
and hand it to the service, and both accept a dry run that prints what they would do
without touching the system.

### Windows (NSSM)

[`scripts/install_service.ps1`](scripts/install_service.ps1) manages a Windows service
named `pcwall` through [NSSM](https://nssm.cc/). Install NSSM first
(`winget install NSSM.NSSM`), then from an elevated PowerShell in the checkout:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1            # install or update, then start
powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -DryRun    # preview, no elevation needed
powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -Action status
powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -Action uninstall
```

The service runs `.venv\Scripts\python.exe` when the virtual environment exists, or the
first `python.exe` on `PATH`; pass `-Python` to choose another interpreter and `-Root` to
point at a checkout elsewhere. Logs rotate at 10 MB. Values from `.env` go into the
service's environment and are shown as `<set>` in a dry run, never in clear.

### Linux (systemd)

[`scripts/install_service.sh`](scripts/install_service.sh) writes
`/etc/systemd/system/pcwall.service` with `Restart=always`, the invoking user, the
checkout as working directory, `EnvironmentFile=-<root>/.env`, and log files under
`logs/`. It needs systemd 240 or newer.

```sh
sudo scripts/install_service.sh                 # install or update, then start
scripts/install_service.sh --dry-run            # print the unit and commands only
sudo scripts/install_service.sh --user wall     # run as a dedicated account
sudo scripts/install_service.sh --uninstall
```

The service runs `.venv/bin/python` when the virtual environment exists, or `python3`
on `PATH`; pass `--python` to choose another interpreter. Check on it with
`systemctl status pcwall` and `tail -f logs/pcwall.log`. If the script was copied from
a Windows checkout and bash complains about `\r`, convert it with `sed -i 's/\r$//'`.

## Talking to the wall from an AI assistant (MCP)

`python -m pcwall.mcp_server` exposes the wall to Claude Desktop, Claude Code, Cursor or
any other [Model Context Protocol](https://modelcontextprotocol.io) client: read the
wall, tick a step, award stars, decide a reward request, set dinner, keep the grocery
list and write the family note. It is a thin HTTP client that needs `PCWALL_KEY` and
goes through the same API and audit ledger as a phone. See [`docs/mcp.md`](docs/mcp.md).

## Development

```sh
python -m pytest tests -q                 # Python suite
node --test tests/test_display.mjs        # front-end display tests (Node 20)
python tools/sanitize_check.py            # publishing gate: no private data in the tree
```

CI runs all three on every push and pull request. The sanitization gate scans every
tracked file for personal names, private hostnames and addresses, birthdates,
coordinate pairs, secret-shaped strings and files that must never be committed
(`.env`, `credentials.json`, tokens, `data/*.json`); keep it green before publishing.

Private terms live only in the gitignored `config/sanitize_denylist.json`. The file
is a UTF-8 JSON object with any subset of `names`, `identifier_names`, `household`,
`birthdays` and `secrets`; each value is a list of non-empty strings. Use
`identifier_names` for names that are also programming identifiers: these match
title-cased standalone words and lowercase quoted string values, while allowing
code identifiers and quoted object keys. For example, using entirely fictional values:

```json
{
  "names": ["zorblat"],
  "identifier_names": ["quux"],
  "household": ["pretend-moon-cottage"],
  "birthdays": ["2099-12-31"],
  "secrets": ["pretend_launch_key"]
}
```

Set `PCWALL_SANITIZE_DENYLIST` to use a list elsewhere; an empty value uses the
default. `--denylist PATH` overrides both the environment and the default path.
Keep the real list local: the gate rejects it if tracked. CI has no private list
and runs only the generic coordinate, secret-shape and forbidden-file rules.
A missing list prints a NOTE and still checks those rules; an invalid list fails.
Before publishing, `python tools/sanitize_check.py --require-private` must pass
locally with your private list in place. This command fails if the list is missing.

```text
pcwall/        server, engine, model, store, access, Google sync, weather, news, MCP
pcwall/static/ the wall's HTML, CSS and JavaScript (no build step)
config/        family.example.json and your gitignored family.json
scripts/       service installers and the Fully Kiosk guide
docs/          display scaling, Google setup, MCP
tests/         pytest suite, Node display tests, sanitization tests
tools/         sanitize_check.py
```

## Security

pcwall is designed for a trusted home network. The parent PIN is stored only as a hash,
the agent key is read from `.env` or `data/pcwall_key.txt` rather than kept in the wall
document, and OAuth tokens stay in local files that git ignores. Read [`SECURITY.md`](SECURITY.md) for the boundaries and for
how to report a vulnerability privately.

## License

MIT. See [`LICENSE`](LICENSE).

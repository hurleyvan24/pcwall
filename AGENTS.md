# AGENTS.md: customising a pcwall family display

pcwall ships with a sample household in `config/family.example.json`. Making the wall
yours means replacing that sample in `config/family.json` and nothing else. The prompt
below is written for a coding agent (Claude, Cursor, ChatGPT, Copilot or similar): paste
it to the agent, then describe your family underneath it. It ships verbatim in this file
so every agent is pointed at the same rules.

## The prompt

> You are customising a pcwall family display. Edit only `config/family.json`. Keep `schema` at 1. Replace the sample members with the family described below, keeping ids lowercase ASCII, one `color` per person from violet, blue, green, orange, slate, stone, and emoji from Unicode 12 or older. Keep at least one parent. Rewrite routines, steps and rewards to match the family's day; a step is worth 1 star unless told otherwise. Put a per-kid bedtime in `overrides`. Set `parent_pin` to the number the parent gives you or leave `1234`. Do not edit any Python, JavaScript or CSS. When done, run `python -m pcwall --check-config` and fix anything it reports.

Underneath the prompt, tell the agent about your family: each person's name, role
(`kid`, `parent` or `shared`), birthdate for kids, favourite colour and emoji, who the
twins are if any, what a school morning, afternoon and bedtime look like, which kids go
to bed later, and the rewards the kids can save stars for.

## Before the agent starts

`config/family.json` is gitignored, so a fresh checkout does not have one. Create it from
the sample first:

```
cp config/family.example.json config/family.json        # macOS / Linux
copy config\family.example.json config\family.json      # Windows
```

Starting the server, or running the check below, performs the same copy automatically
when the file is missing and logs a one-line notice.

## `--check-config`

`python -m pcwall --check-config` runs `load_family` and prints the resolved members,
routines and rewards as a table, so the agent and the human can both confirm the result
without starting the server. The same check is available as
`python -m pcwall.config --check-config`, and `--config <path>` points it at a file other
than `config/family.json`.

- Exit code 0: the file is valid. The table lists every member with their colour and
  emoji, every routine as each kid will see it (per-kid `overrides` already applied) and
  every reward with its star price. The PIN is never printed.
- Exit code 1: the file is invalid. The message names the exact JSON path and the rule it
  breaks, for example:

  ```
  config/family.json: members[2].twin: twin 'nobody' must be the id of another kid
  ```

  Fix that path, run the check again, and repeat until it exits 0.

## Rules the check enforces

- `schema` is `1`.
- `household.name` is non-empty and `household.timezone` is an IANA zone such as
  `America/New_York` or `Europe/Berlin`. `household.school.kindergarten_cutoff` is `MM-DD`
  and `household.school.year_start_month` is `1` to `12`.
- Every `id` (members, routines, steps, rewards) matches `^[a-z][a-z0-9_]{0,23}$` and is
  unique within its list. Step ids only need to be unique inside their routine.
- `role` is `kid`, `parent` or `shared`; there is at least one kid and at least one parent.
- `color` is one of `violet`, `blue`, `green`, `orange`, `slate`, `stone`.
- Kids have a `birthdate` in `YYYY-MM-DD`; it drives the school grade shown on the wall.
- `twin` names another member whose role is `kid`.
- `days` is a non-empty subset of `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`; `start`
  and `end` are 24-hour `HH:MM`.
- A step's `stars` defaults to `1`; a reward's `stars` is its price and is required.
- `overrides` is keyed by kid id, then routine id, and may change `title`, `emoji`,
  `days`, `start`, `end` or `steps` for that kid only.
- `parent_pin` is a string of 4 to 8 digits. It is hashed with PBKDF2-SHA256 into the
  wall's data document on the first run; after that the plaintext in `config/family.json`
  is ignored, and the wall keeps a reminder up until the sample `1234` is changed.

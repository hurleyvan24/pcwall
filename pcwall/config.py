"""Family configuration for pcwall: load, validate and seed from ``config/family.json``.

The wall is generic. Every member, routine, reward and the parent PIN come from
``config/family.json`` (schema 1), never from constants in the code. This module

* loads and validates that file (``load_family``) and raises ``ConfigError`` whose
  ``path`` names the exact JSON path of the offending value (``members[2].twin``,
  ``household.timezone``), so start-up stops before any document is written;
* copies ``family.example.json`` into place when ``family.json`` is missing
  (``ensure_family_file``);
* hashes the parent PIN with PBKDF2-SHA256 (``hash_pin`` / ``verify_pin``);
* seeds the initial wall document (``default_document``) and exposes the members as
  plain dicts (``family_to_members``);
* prints the resolved configuration as a table for ``--check-config``
  (``print_config_table`` / ``main``).

Nothing here touches the network or the data directory.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "ConfigError",
    "Family",
    "SchoolRules",
    "PALETTE",
    "ID_PATTERN",
    "DAYS",
    "ROLES",
    "load_family",
    "parse_family",
    "ensure_family_file",
    "default_config_dir",
    "hash_pin",
    "verify_pin",
    "is_pin_hash",
    "default_document",
    "default_settings",
    "family_to_members",
    "print_config_table",
    "check_config",
    "main",
]

log = logging.getLogger("pcwall.config")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1
FAMILY_FILENAME = "family.json"
EXAMPLE_FILENAME = "family.example.json"
SAMPLE_PIN = "1234"

PALETTE = frozenset({"violet", "blue", "green", "orange", "slate", "stone"})
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
ROLES = ("kid", "parent", "shared")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
OVERRIDABLE = ("title", "emoji", "days", "start", "end", "steps")
TIME_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_DAY_PATTERN = re.compile(r"^[0-9]{2}-[0-9]{2}$")
PIN_PATTERN = re.compile(r"^[0-9]{4,8}$")

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ROUNDS = 200_000
PBKDF2_SALT_BYTES = 16
PBKDF2_ROUNDS_CEILING = 10_000_000  # refuse to verify a stored hash that would take forever

DEFAULT_NEWS_FEEDS = ({"title": "NPR", "url": "https://feeds.npr.org/1001/rss.xml"},)


# --- errors and data types ------------------------------------------------------------


class ConfigError(Exception):
    """A problem in family.json. ``path`` is the JSON path of the value (``$`` is the file)."""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


@dataclass(frozen=True)
class SchoolRules:
    """How the wall turns a birthdate into a grade: a child who turns five on or before
    ``kindergarten_cutoff`` (MM-DD) starts kindergarten in the year that begins in
    ``year_start_month``."""

    kindergarten_cutoff: str = "09-01"
    year_start_month: int = 8

    @property
    def cutoff(self) -> tuple[int, int]:
        month, day = self.kindergarten_cutoff.split("-")
        return int(month), int(day)


@dataclass(frozen=True)
class Family:
    """The validated, normalised content of family.json.

    ``members``, ``routines`` and ``rewards`` are JSON-shaped dicts in file order. Treat
    them as read-only; ``family_to_members`` and ``routines_for`` hand out copies.
    """

    schema: int
    name: str
    timezone: str
    school: SchoolRules
    members: list[dict]
    routines: list[dict]
    overrides: dict[str, dict[str, dict]]
    rewards: list[dict]
    parent_pin: str
    source: Path | None = None

    @property
    def kids(self) -> list[dict]:
        return [m for m in self.members if m["role"] == "kid"]

    @property
    def parents(self) -> list[dict]:
        return [m for m in self.members if m["role"] == "parent"]

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def member(self, member_id: str) -> dict:
        for member in self.members:
            if member["id"] == member_id:
                return member
        raise KeyError(member_id)

    def routines_for(self, member_id: str) -> list[dict]:
        """Every routine as this kid sees it, with that kid's overrides applied.

        Returns fresh deep copies; non-kid members have no routines. Unknown ids raise
        ``KeyError`` so a typo surfaces instead of rendering an empty card.
        """
        member = self.member(member_id)
        if member["role"] != "kid":
            return []
        patches = self.overrides.get(member_id, {})
        resolved = []
        for routine in self.routines:
            item = copy.deepcopy(routine)
            item.update(copy.deepcopy(patches.get(routine["id"], {})))
            resolved.append(item)
        return resolved


# --- validation primitives -------------------------------------------------------------


def _fail(path: str, message: str) -> None:
    raise ConfigError(path, message)


def _join(parent: str, key: str) -> str:
    return key if parent in ("", "$") else f"{parent}.{key}"


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    return {dict: "object", list: "array", str: "string", int: "integer", float: "number"}.get(
        type(value), type(value).__name__
    )


def _required(container: dict, key: str, parent: str) -> tuple[Any, str]:
    path = _join(parent, key)
    if key not in container:
        _fail(path, "is required")
    return container[key], path


def _expect_object(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        _fail(path, f"must be an object (got {_kind(value)})")
    return value


def _expect_array(value: Any, path: str) -> list:
    if not isinstance(value, list):
        _fail(path, f"must be an array (got {_kind(value)})")
    return value


def _expect_string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        _fail(path, f"must be a string (got {_kind(value)})")
    if nonempty and not value.strip():
        _fail(path, "must not be empty")
    return value


def _expect_integer(value: Any, path: str, *, lo: int | None = None, hi: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, f"must be an integer (got {_kind(value)})")
    if lo is not None and hi is not None and not lo <= value <= hi:
        _fail(path, f"must be between {lo} and {hi} (got {value})")
    if lo is not None and value < lo:
        _fail(path, f"must be at least {lo} (got {value})")
    if hi is not None and value > hi:
        _fail(path, f"must be at most {hi} (got {value})")
    return value


def _expect_id(value: Any, path: str, seen: set[str], what: str) -> str:
    text = _expect_string(value, path)
    if not ID_PATTERN.match(text):
        _fail(path, f"must match {ID_PATTERN.pattern} (lowercase ASCII, got {text!r})")
    if text in seen:
        _fail(path, f"duplicate {what} id {text!r}")
    seen.add(text)
    return text


def _expect_time(value: Any, path: str) -> str:
    text = _expect_string(value, path)
    if not TIME_PATTERN.match(text):
        _fail(path, f"must be a 24-hour time HH:MM (got {text!r})")
    return text


def _expect_date(value: Any, path: str) -> str:
    text = _expect_string(value, path)
    if not DATE_PATTERN.match(text):
        _fail(path, f"must be a date YYYY-MM-DD (got {text!r})")
    try:
        date.fromisoformat(text)
    except ValueError:
        _fail(path, f"is not a real calendar date ({text!r})")
    return text


def _expect_month_day(value: Any, path: str) -> str:
    text = _expect_string(value, path)
    if not MONTH_DAY_PATTERN.match(text):
        _fail(path, f"must be MM-DD (got {text!r})")
    try:
        date.fromisoformat(f"2000-{text}")  # 2000 is a leap year, so 02-29 is accepted
    except ValueError:
        _fail(path, f"is not a real month and day ({text!r})")
    return text


def _expect_days(value: Any, path: str) -> list[str]:
    items = _expect_array(value, path)
    if not items:
        _fail(path, f"must list at least one day of {', '.join(DAYS)}")
    days: list[str] = []
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        day = _expect_string(item, item_path)
        if day not in DAYS:
            _fail(item_path, f"must be one of {', '.join(DAYS)} (got {day!r})")
        if day in days:
            _fail(item_path, f"duplicate day {day!r}")
        days.append(day)
    return days


def _expect_steps(value: Any, path: str) -> list[dict]:
    items = _expect_array(value, path)
    if not items:
        _fail(path, "must list at least one step")
    seen: set[str] = set()
    steps: list[dict] = []
    for index, raw in enumerate(items):
        step_path = f"{path}[{index}]"
        obj = _expect_object(raw, step_path)
        step_id = _expect_id(*_required(obj, "id", step_path), seen, "step")
        title = _expect_string(*_required(obj, "title", step_path))
        emoji = _expect_string(obj.get("emoji", ""), f"{step_path}.emoji", nonempty=False)
        stars = _expect_integer(obj.get("stars", 1), f"{step_path}.stars", lo=0)
        steps.append({"id": step_id, "title": title, "emoji": emoji, "stars": stars})
    return steps


# --- section parsers --------------------------------------------------------------------


def _parse_household(value: Any) -> tuple[str, str, SchoolRules]:
    obj = _expect_object(value, "household")
    name = _expect_string(*_required(obj, "name", "household"))
    timezone = _expect_string(*_required(obj, "timezone", "household"))
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _fail(
            "household.timezone",
            f"unknown IANA timezone {timezone!r} (for example America/New_York; "
            "if no zone works, install the tzdata package)",
        )
    school = _expect_object(obj.get("school", {}), "household.school")
    cutoff = _expect_month_day(
        school.get("kindergarten_cutoff", SchoolRules.kindergarten_cutoff),
        "household.school.kindergarten_cutoff",
    )
    year_start = _expect_integer(
        school.get("year_start_month", SchoolRules.year_start_month),
        "household.school.year_start_month",
        lo=1,
        hi=12,
    )
    return name, timezone, SchoolRules(kindergarten_cutoff=cutoff, year_start_month=year_start)


def _parse_members(value: Any) -> list[dict]:
    items = _expect_array(value, "members")
    if not items:
        _fail("members", "must list at least one member")
    seen: set[str] = set()
    members: list[dict] = []
    for index, raw in enumerate(items):
        path = f"members[{index}]"
        obj = _expect_object(raw, path)
        member_id = _expect_id(*_required(obj, "id", path), seen, "member")
        name = _expect_string(*_required(obj, "name", path))
        role = _expect_string(*_required(obj, "role", path))
        if role not in ROLES:
            _fail(f"{path}.role", f"must be one of {', '.join(ROLES)} (got {role!r})")
        color = _expect_string(*_required(obj, "color", path))
        if color not in PALETTE:
            _fail(f"{path}.color", f"must be one of {', '.join(sorted(PALETTE))} (got {color!r})")
        birthdate = obj.get("birthdate")
        if birthdate is None:
            if role == "kid":
                _fail(f"{path}.birthdate", "is required for a kid (YYYY-MM-DD)")
        else:
            birthdate = _expect_date(birthdate, f"{path}.birthdate")
        emoji = _expect_string(obj.get("emoji", ""), f"{path}.emoji", nonempty=False)
        twin = obj.get("twin")
        if twin is not None:
            twin = _expect_string(twin, f"{path}.twin")
        aliases = []
        for alias_index, alias in enumerate(_expect_array(obj.get("aliases", []), f"{path}.aliases")):
            aliases.append(_expect_string(alias, f"{path}.aliases[{alias_index}]"))
        members.append(
            {
                "id": member_id,
                "name": name,
                "role": role,
                "color": color,
                "emoji": emoji,
                "birthdate": birthdate,
                "twin": twin,
                "aliases": aliases,
            }
        )

    by_id = {member["id"]: member for member in members}
    for index, member in enumerate(members):
        twin = member["twin"]
        if twin is None:
            continue
        if member["role"] != "kid":
            _fail(f"members[{index}].twin", "only a kid can have a twin")
        other = by_id.get(twin)
        if other is None or other is member or other["role"] != "kid":
            _fail(f"members[{index}].twin", f"twin {twin!r} must be the id of another kid")
    if not any(member["role"] == "kid" for member in members):
        _fail("members", "at least one member with role 'kid' is required")
    if not any(member["role"] == "parent" for member in members):
        _fail("members", "at least one member with role 'parent' is required")
    return members


def _parse_routines(value: Any) -> list[dict]:
    items = _expect_array([] if value is None else value, "routines")
    seen: set[str] = set()
    routines: list[dict] = []
    for index, raw in enumerate(items):
        path = f"routines[{index}]"
        obj = _expect_object(raw, path)
        routines.append(
            {
                "id": _expect_id(*_required(obj, "id", path), seen, "routine"),
                "title": _expect_string(*_required(obj, "title", path)),
                "emoji": _expect_string(obj.get("emoji", ""), f"{path}.emoji", nonempty=False),
                "days": _expect_days(*_required(obj, "days", path)),
                "start": _expect_time(*_required(obj, "start", path)),
                "end": _expect_time(*_required(obj, "end", path)),
                "steps": _expect_steps(*_required(obj, "steps", path)),
            }
        )
    return routines


_OVERRIDE_VALIDATORS: dict[str, Callable[[Any, str], Any]] = {
    "title": lambda value, path: _expect_string(value, path),
    "emoji": lambda value, path: _expect_string(value, path, nonempty=False),
    "days": _expect_days,
    "start": _expect_time,
    "end": _expect_time,
    "steps": _expect_steps,
}


def _parse_overrides(value: Any, kid_ids: Iterable[str], routine_ids: Iterable[str]) -> dict:
    kids = set(kid_ids)
    routines = set(routine_ids)
    obj = _expect_object({} if value is None else value, "overrides")
    overrides: dict[str, dict[str, dict]] = {}
    for kid_id, per_kid in obj.items():
        kid_path = f"overrides.{kid_id}"
        if kid_id not in kids:
            _fail(kid_path, f"{kid_id!r} is not the id of a kid")
        overrides[kid_id] = {}
        for routine_id, patch in _expect_object(per_kid, kid_path).items():
            routine_path = f"{kid_path}.{routine_id}"
            if routine_id not in routines:
                _fail(routine_path, f"{routine_id!r} is not a routine id")
            clean: dict[str, Any] = {}
            for key, raw in _expect_object(patch, routine_path).items():
                key_path = f"{routine_path}.{key}"
                validator = _OVERRIDE_VALIDATORS.get(key)
                if validator is None:
                    _fail(key_path, f"unknown override field; allowed: {', '.join(OVERRIDABLE)}")
                clean[key] = validator(raw, key_path)
            overrides[kid_id][routine_id] = clean
    return overrides


def _parse_rewards(value: Any) -> list[dict]:
    items = _expect_array([] if value is None else value, "rewards")
    seen: set[str] = set()
    rewards: list[dict] = []
    for index, raw in enumerate(items):
        path = f"rewards[{index}]"
        obj = _expect_object(raw, path)
        rewards.append(
            {
                "id": _expect_id(*_required(obj, "id", path), seen, "reward"),
                "title": _expect_string(*_required(obj, "title", path)),
                "emoji": _expect_string(obj.get("emoji", ""), f"{path}.emoji", nonempty=False),
                "stars": _expect_integer(*_required(obj, "stars", path), lo=1),
            }
        )
    return rewards


def _parse_pin(value: Any) -> str:
    if not isinstance(value, str):
        _fail("parent_pin", f"must be a string of 4 to 8 digits, quoted (got {_kind(value)})")
    if not PIN_PATTERN.match(value):
        _fail("parent_pin", "must be 4 to 8 digits")
    return value


# --- public loading API -----------------------------------------------------------------


def parse_family(data: Any, source: Path | None = None) -> Family:
    """Validate an already-decoded family document and return the ``Family``.

    Sections are checked in file order (schema, household, members, routines, overrides,
    rewards, parent_pin); the first problem raises ``ConfigError`` with its JSON path.
    """
    root = _expect_object(data, "$")
    schema = _expect_integer(*_required(root, "schema", "$"))
    if schema != SCHEMA_VERSION:
        _fail("schema", f"must be {SCHEMA_VERSION} (got {schema})")
    name, timezone, school = _parse_household(_required(root, "household", "$")[0])
    members = _parse_members(_required(root, "members", "$")[0])
    routines = _parse_routines(root.get("routines"))
    overrides = _parse_overrides(
        root.get("overrides"),
        (m["id"] for m in members if m["role"] == "kid"),
        (r["id"] for r in routines),
    )
    rewards = _parse_rewards(root.get("rewards"))
    parent_pin = _parse_pin(_required(root, "parent_pin", "$")[0])
    return Family(
        schema=schema,
        name=name,
        timezone=timezone,
        school=school,
        members=members,
        routines=routines,
        overrides=overrides,
        rewards=rewards,
        parent_pin=parent_pin,
        source=source,
    )


def load_family(path: Path | str) -> Family:
    """Read and validate ``family.json``. Never writes anything."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        _fail("$", f"config file not found: {source}")
    except OSError as exc:
        _fail("$", f"cannot read {source}: {exc}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _fail("$", f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})")
    return parse_family(data, source=source)


def default_config_dir() -> Path:
    """``PCWALL_CONFIG_DIR`` when set, else ``<repo>/config``."""
    override = os.environ.get("PCWALL_CONFIG_DIR")
    return Path(override) if override else ROOT / "config"


def ensure_family_file(config_dir: Path | str | None = None) -> Path:
    """Return the path of ``family.json`` inside ``config_dir``, creating it from
    ``family.example.json`` when it is missing.

    The example is taken from ``config_dir`` first and from the repository's ``config/``
    otherwise, so a service pointed at an empty directory still gets the sample. An
    existing ``family.json`` is never touched. Logs a one-line notice when it copies.
    """
    directory = Path(config_dir) if config_dir is not None else default_config_dir()
    target = directory / FAMILY_FILENAME
    if target.exists():
        return target
    candidates = (directory / EXAMPLE_FILENAME, ROOT / "config" / EXAMPLE_FILENAME)
    example = next((candidate for candidate in candidates if candidate.is_file()), None)
    if example is None:
        raise FileNotFoundError(
            f"{EXAMPLE_FILENAME} not found in {directory} or {ROOT / 'config'}; "
            "cannot create family.json"
        )
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, target)
    log.info("%s was missing; copied %s to %s", FAMILY_FILENAME, example, target)
    return target


# --- parent PIN -------------------------------------------------------------------------


def hash_pin(pin: str, salt: bytes | None = None) -> str:
    """PBKDF2-SHA256 of the PIN: ``pbkdf2_sha256$<rounds>$<salt hex>$<digest hex>``."""
    if salt is None:
        salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"{PBKDF2_ALGORITHM}${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def is_pin_hash(value: Any) -> bool:
    """True when ``value`` has the shape ``hash_pin`` produces."""
    if not isinstance(value, str):
        return False
    parts = value.split("$")
    if len(parts) != 4 or parts[0] != PBKDF2_ALGORITHM or not parts[1].isdigit():
        return False
    try:
        return bool(bytes.fromhex(parts[2])) and bool(bytes.fromhex(parts[3]))
    except ValueError:
        return False


def verify_pin(pin: str, stored: Any) -> bool:
    """Constant-time check of ``pin`` against a stored ``hash_pin`` value."""
    if not is_pin_hash(stored):
        return False
    _, rounds_text, salt_hex, digest_hex = stored.split("$")
    rounds = int(rounds_text)
    if not 1 <= rounds <= PBKDF2_ROUNDS_CEILING:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), bytes.fromhex(salt_hex), rounds)
    return hmac.compare_digest(actual, bytes.fromhex(digest_hex))


# --- document seeding -------------------------------------------------------------------


def family_to_members(family: Family) -> list[dict]:
    """The members as JSON-shaped dicts (deep copies, file order)."""
    return copy.deepcopy(family.members)


def default_settings(family: Family) -> dict:
    """Initial ``settings`` section. ``parent_pin`` is stored hashed, never in clear."""
    return {
        "screensaver": {"enabled": True, "idle_minutes": 10},
        "night": {"start": "21:00", "end": "06:00"},
        "stars": {"max_award": 5, "streak_milestones": {}},
        "calendar": {"enabled": True, "days_ahead": 7},
        "photos": {"enabled": True, "interval_seconds": 30},
        "groceries": {"enabled": True},
        "display": {"default_preset": "auto", "kid_columns": "auto"},
        "weather": {"enabled": True, "units": "fahrenheit", "refresh_minutes": 30},
        "news": {
            "enabled": True,
            "feeds": [dict(feed) for feed in DEFAULT_NEWS_FEEDS],
            "refresh_minutes": 15,
            "max_items": 30,
            "ticker_speed": 60,
        },
        "parent_pin": hash_pin(family.parent_pin),
        "parent_pin_is_sample": family.parent_pin == SAMPLE_PIN,
    }


def default_document(family: Family, now: datetime) -> dict:
    """The complete initial wall document for a family that has never run before.

    Routines are flattened to one entry per kid and routine (``id`` = ``<kid>.<routine>``)
    with that kid's overrides applied, so the store can match them by id later. The
    plaintext PIN is hashed here once; after this the config value is ignored.
    """
    stamp = now.isoformat()
    kids = family.kids
    routines: list[dict] = []
    for kid in kids:
        for routine in family.routines_for(kid["id"]):
            routines.append(
                {
                    "id": f"{kid['id']}.{routine['id']}",
                    "member": kid["id"],
                    "routine": routine["id"],
                    "title": routine["title"],
                    "emoji": routine["emoji"],
                    "days": routine["days"],
                    "start": routine["start"],
                    "end": routine["end"],
                    "steps": routine["steps"],
                }
            )
    return {
        "schema": SCHEMA_VERSION,
        "revision": 0,
        "created_at": stamp,
        "updated_at": stamp,
        "household": {
            "name": family.name,
            "timezone": family.timezone,
            "school": {
                "kindergarten_cutoff": family.school.kindergarten_cutoff,
                "year_start_month": family.school.year_start_month,
            },
        },
        "members": family_to_members(family),
        "routines": routines,
        "rewards": copy.deepcopy(family.rewards),
        "completions": {},
        "stars": {"balances": {kid["id"]: 0 for kid in kids}, "ledger": []},
        "reward_requests": [],
        "meals": {},
        "groceries": [],
        "calendar": {"events": [], "synced_at": None},
        "notes": {"text": "", "updated_at": None},
        "photos": {"items": [], "synced_at": None},
        "weather": {"location": None, "forecast": None, "fetched_at": None},
        "news": {"items": [], "fetched_at": None},
        "settings": default_settings(family),
    }


# --- --check-config ---------------------------------------------------------------------


def _table(write: Callable[[str], Any], headers: tuple[str, ...], rows: list[tuple]) -> None:
    cells = [tuple(str(cell) for cell in row) for row in rows]
    widths = [len(header) for header in headers]
    for row in cells:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    write("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)).rstrip() + "\n")
    write("  ".join("-" * width for width in widths) + "\n")
    for row in cells:
        write("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() + "\n")
    if not cells:
        write("(none)\n")


def print_config_table(family: Family, out: TextIO | None = None) -> None:
    """Print members, per-kid routines (overrides applied) and rewards. Never the PIN."""
    write = (out or sys.stdout).write
    write(
        f"Household: {family.name}  timezone: {family.timezone}  "
        f"kindergarten cutoff: {family.school.kindergarten_cutoff}  "
        f"school year starts: month {family.school.year_start_month}\n"
    )
    write("\nMembers\n")
    _table(
        write,
        ("id", "name", "role", "color", "emoji", "birthdate", "twin", "aliases"),
        [
            (
                m["id"],
                m["name"],
                m["role"],
                m["color"],
                m["emoji"] or "-",
                m["birthdate"] or "-",
                m["twin"] or "-",
                ", ".join(m["aliases"]) or "-",
            )
            for m in family.members
        ],
    )
    write("\nRoutines (one row per kid; overrides already applied)\n")
    rows = []
    for kid in family.kids:
        for routine in family.routines_for(kid["id"]):
            rows.append(
                (
                    kid["id"],
                    routine["id"],
                    ",".join(routine["days"]),
                    f"{routine['start']}-{routine['end']}",
                    len(routine["steps"]),
                    sum(step["stars"] for step in routine["steps"]),
                    " / ".join(step["title"] for step in routine["steps"]),
                )
            )
    _table(write, ("kid", "routine", "days", "time", "steps", "stars", "step titles"), rows)
    write("\nRewards\n")
    _table(
        write,
        ("id", "title", "emoji", "stars"),
        [(r["id"], r["title"], r["emoji"] or "-", r["stars"]) for r in family.rewards],
    )
    if family.parent_pin == SAMPLE_PIN:
        write("\nparent_pin: still the sample value; change it before the wall goes on the kitchen counter\n")
    else:
        write("\nparent_pin: set\n")


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def check_config(path: Path | str | None = None, *, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Load ``path`` (default: ``config/family.json``, created from the example when
    missing), print the resolved table and return 0; on a problem print
    ``<file>: <json path>: <message>`` to ``err`` and return 1."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        target = Path(path) if path is not None else ensure_family_file()
    except (FileNotFoundError, OSError) as exc:
        err.write(f"error: {exc}\n")
        return 1
    try:
        family = load_family(target)
    except ConfigError as exc:
        err.write(f"{_display_path(target)}: {exc.path}: {exc.message}\n")
        return 1
    print_config_table(family, out)
    out.write(f"\nOK: {_display_path(target)} is valid.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """``python -m pcwall.config --check-config [--config PATH]``."""
    parser = argparse.ArgumentParser(
        prog="python -m pcwall.config",
        description="Validate config/family.json and print the resolved members, routines and rewards.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the family file and print it as a table (the default action)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=f"family file to check (default: {default_config_dir() / FAMILY_FILENAME})",
    )
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")  # emoji on a legacy Windows console must not crash the check
            except (ValueError, OSError):
                pass
    return check_config(args.config)


if __name__ == "__main__":
    sys.exit(main())

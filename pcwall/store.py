"""data/pcwall.json: one JSON document with one writer, the FamilyWall service.

Every change runs as a transaction on a deep copy, so a change that raises leaves
memory and disk exactly as they were. Saves are atomic (temp file, fsync,
os.replace), the first save of each day keeps yesterday's file in
data/pcwall_backups/, and a file that will not parse is moved aside, never
overwritten. See docs/SKYLIGHT-CLONE-DESIGN.md section 2.3.

A wall that has never run is seeded from config.default_document(family, now), so
every member, routine, reward and the hashed parent PIN come from config/family.json.
A wall that has run before keeps its document; _merge_document only brings in what
this build and this family.json introduce.
"""

import copy
import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from pcwall import config, model

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "data", "pcwall.json")
KEEP_BACKUPS = 30
ARCHIVE_NAME = "pcwall_ledger_archive.jsonl"
CONFIG_DIR_ENV = "PCWALL_CONFIG_DIR"

# What follows family.json on a document that already exists, matched by id. A member
# keeps anything else the wall stored on it, a routine keeps "active" and whatever a
# parent set, and a reward keeps everything: parents edit the catalog on the wall.
MEMBER_FIELDS = ("name", "role", "color", "emoji", "birthdate", "twin", "aliases")
ROUTINE_FIELDS = ("member", "routine", "title", "emoji", "days", "start", "end", "steps")


class StoreError(Exception):
    """The store cannot start without risking the family's data."""


def config_dir_for(path) -> Path:
    """Where the store at ``path`` finds family.json: $PCWALL_CONFIG_DIR when set, else
    the config/ directory beside the data/ directory that holds the document (the
    repository layout: <root>/data/pcwall.json and <root>/config/family.json)."""
    override = os.environ.get(CONFIG_DIR_ENV)
    return Path(override) if override else Path(path).resolve().parent.parent / "config"


def load_family_for(path) -> config.Family:
    """load_family(ensure_family_file(...)) for the store at ``path``: the sample is
    copied into place when family.json is missing, and a file that does not validate
    raises ConfigError before any document is written."""
    return config.load_family(config.ensure_family_file(config_dir_for(path)))


def schema_of(state: dict):
    """The document's schema number, from ``schema`` (documents seeded from the family
    config) or ``schema_version`` (older ones); None when there is no usable number."""
    value = state.get("schema", state.get("schema_version"))
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sync_by_id(items: list, defaults: list, fields: tuple) -> None:
    """Add every default whose id the list lacks, after whatever is there; on the ones it
    has, copy ``fields`` over (nothing, for rewards) and leave every other key alone."""
    found = {item.get("id"): item for item in items if isinstance(item, dict)}
    for default in defaults:
        current = found.get(default["id"])
        if current is None:
            items.append(copy.deepcopy(default))
            continue
        for key in fields:
            if key in default:
                current[key] = copy.deepcopy(default[key])


def _fill_defaults(target: dict, defaults: dict) -> None:
    """Add keys a newer build introduced, recursively; never overwrite what is there."""
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _fill_defaults(target[key], value)


def _merge_document(state: dict, defaults: dict) -> None:
    """Bring a document that already exists in line with this family.json and this build
    without touching what the family did on the wall.

    * Keys a newer build introduced are added and never overwritten, so weather.location,
      settings.display and the hashed parent PIN survive a restart.
    * household follows the config.
    * Members and routines from the config are synced by id (a changed colour or bedtime
      shows up on the next start) and missing ones are added; a member or routine only the
      document knows (a kid dropped from the config, a routine a parent added) stays, and a
      routine's "active" flag stays. The per-routine bonus an older build paid is zeroed.
    * Rewards missing from the catalog are added; the rest is the parents' to edit.
    * Streak milestones are always cleared: the day's bonus is the only bonus.
    """
    for key, value in defaults.items():
        if key not in state:
            state[key] = copy.deepcopy(value)
        elif key == "household":
            state[key] = copy.deepcopy(value)
        elif key == "members" and isinstance(state[key], list):
            _sync_by_id(state[key], value, MEMBER_FIELDS)
        elif key == "routines" and isinstance(state[key], list):
            _sync_by_id(state[key], value, ROUTINE_FIELDS)
        elif key == "rewards" and isinstance(state[key], list):
            _sync_by_id(state[key], value, ())
        elif isinstance(value, dict) and isinstance(state[key], dict):
            _fill_defaults(state[key], value)
    for routine in state.get("routines", []):
        if isinstance(routine, dict) and "bonus" in routine:
            routine["bonus"] = 0
    settings = state.get("settings")
    if isinstance(settings, dict):
        if isinstance(settings.get("stars"), dict):
            settings["stars"]["streak_milestones"] = {}
        if not settings.get("parent_pin"):
            settings["parent_pin"] = defaults["settings"]["parent_pin"]


class Store:
    """The wall's one document.

    ``family`` is the validated family.json; when not given it is loaded from the config
    directory that belongs to ``path`` (see config_dir_for), copying the sample into
    place first if needed. ``now`` stamps a document created or recovered on this start
    and defaults to the clock; ``clock`` is what transactions ask the time from.
    """

    def __init__(self, path=DEFAULT_PATH, family=None, now=None, *, clock=None,
                 keep_backups: int = KEEP_BACKUPS):
        self.path = os.path.abspath(os.fspath(path))
        self.backup_dir = os.path.join(os.path.dirname(self.path), "pcwall_backups")
        self.family = family if family is not None else load_family_for(self.path)
        self.zone = self.family.zone
        self.clock = clock or (lambda: datetime.now(self.zone))
        self.keep_backups = keep_backups
        self.recovered_from = None
        self._lock = threading.RLock()
        self._listeners = []
        self._state = self._load(now if now is not None else self.clock())

    def now(self) -> datetime:
        return self.clock()

    def current(self) -> dict:
        """The live state. Read-only: transact() swaps in a new dict, never edits this one."""
        return self._state

    @property
    def revision(self) -> int:
        return self._state["revision"]

    def subscribe(self, listener) -> None:
        """listener({"revision": n, "kinds": [...]}) runs after every saved change."""
        self._listeners.append(listener)

    def archive(self, entries: list) -> int:
        """Append rolled-up ledger entries, one JSON object a line, to
        data/pcwall_ledger_archive.jsonl: the long-term bank statement. Called inside a
        transaction before the save, so a failed write aborts the roll-up; a save that fails
        afterwards can leave a repeated line, which readers drop by entry id."""
        if not entries:
            return 0
        path = os.path.join(os.path.dirname(self.path), ARCHIVE_NAME)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries))
            handle.flush()
            os.fsync(handle.fileno())
        return len(entries)

    def transact(self, change, *, kinds=()):
        """Run change(draft, now) on a copy; save and publish only if it returned normally
        and actually changed something. Returns whatever change returned."""
        with self._lock:
            draft = copy.deepcopy(self._state)
            now = self.now()
            result = change(draft, now)
            if draft == self._state:
                return result
            draft["revision"] = self._state["revision"] + 1
            draft["updated_at"] = now.isoformat()
            self._backup_once_a_day(now)
            self._write(draft)
            self._state = draft
            event = {"revision": draft["revision"], "kinds": list(kinds)}
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as exc:  # a broken listener must not undo a saved change
                print(f"family wall: listener failed: {type(exc).__name__}: {exc}")
        return result

    def _load(self, now: datetime) -> dict:
        defaults = config.default_document(self.family, now)
        if not os.path.exists(self.path):
            state = self._from_backups(defaults)
            if state is None:
                state = defaults
                self._write(state)
            return state
        try:
            with open(self.path, encoding="utf-8") as handle:
                state = json.load(handle)
            if not isinstance(state, dict) or schema_of(state) is None:
                raise ValueError("not a family wall document")
        except ValueError as exc:
            return self._recover(now, exc, defaults)
        if schema_of(state) > model.SCHEMA_VERSION:
            raise StoreError(f"{self.path} was written by a newer Family Wall "
                             f"(schema {schema_of(state)}); update the code before starting.")
        _merge_document(state, defaults)
        return state

    def _from_backups(self, defaults: dict):
        """The newest backup that parses and matches this schema, merged and written back
        as the document; None when there is none."""
        for name in sorted(self._backups(), reverse=True):
            try:
                with open(os.path.join(self.backup_dir, name), encoding="utf-8") as handle:
                    state = json.load(handle)
            except ValueError:
                continue
            if isinstance(state, dict) and schema_of(state) == model.SCHEMA_VERSION:
                _merge_document(state, defaults)
                self.recovered_from = name
                self._write(state)
                return state
        return None

    def _recover(self, now: datetime, error: Exception, defaults: dict) -> dict:
        aside = f"{self.path}.corrupt-{now:%Y%m%dT%H%M%S}"
        os.replace(self.path, aside)
        print(f"family wall: {self.path} did not parse ({error}); kept it as {aside}")
        state = self._from_backups(defaults)
        if state is None:
            self.recovered_from = "new"
            state = defaults
            self._write(state)
        return state

    def _backups(self) -> list:
        if not os.path.isdir(self.backup_dir):
            return []
        return [n for n in os.listdir(self.backup_dir) if n.startswith("pcwall-") and n.endswith(".json")]

    def _backup_once_a_day(self, now: datetime) -> None:
        target = os.path.join(self.backup_dir, f"pcwall-{now:%Y-%m-%d}.json")
        if os.path.exists(target) or not os.path.exists(self.path):
            return
        os.makedirs(self.backup_dir, exist_ok=True)
        with open(self.path, "rb") as source, open(target, "wb") as copy_:
            copy_.write(source.read())
        for name in sorted(self._backups())[:-self.keep_backups]:
            os.remove(os.path.join(self.backup_dir, name))

    def _write(self, state: dict) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".pcwall-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(10):     # about 2 s in all
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:   # Windows: a virus scanner or backup tool has it open
                    if attempt == 9:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


FamilyWallStore = Store   # the name server.py imports

"""POST /api/do {"op": name, "args": {...}}: every change, and every read a parent asks
for, by name.

Each op names the roles allowed to call it. Ops with in_transaction=True run inside one
store transaction as fn(state, args, caller, now, ctx); the rest run as
fn(store, args, caller, ctx) and write nothing. `caller` is an access.Principal and `ctx`
the server's context (pairing, pin_limiter, bus). Nothing here talks to anything outside
the wall: the family's members, routines and rewards come from the document the store
seeded from config/family.json.
"""

import re
from copy import deepcopy
from dataclasses import dataclass

from pcwall import access, config, engine
from pcwall.engine import WallError

EVERYONE = ("kiosk", "parent", "agent")
PARENTS = ("parent", "agent")
# The settings sections a parent may change with settings_set, and the two that no op but
# set_pin may touch: the hashed PIN never travels through a generic settings write.
EDITABLE_SETTINGS = ("screensaver", "night", "stars", "calendar", "photos", "groceries", "display",
                     "weather", "news")
PROTECTED_SETTINGS = ("parent_pin", "parent_pin_is_sample")
SETTING_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
SETTING_TEXT_MAX = 500
SETTING_LIST_MAX = 50
SETTING_DEPTH_MAX = 3
# Fields the wall relies on, checked beyond "is JSON": a bad clock would break night mode.
SETTING_RULES = {
    ("night", "start"): lambda v: isinstance(v, str) and config.TIME_PATTERN.fullmatch(v) is not None,
    ("night", "end"): lambda v: isinstance(v, str) and config.TIME_PATTERN.fullmatch(v) is not None,
    ("stars", "max_award"): lambda v: type(v) is int and 1 <= v <= 100,
    ("screensaver", "idle_minutes"): lambda v: type(v) is int and 1 <= v <= 240,
    # Whole hours only, never null or text: engine.maintain turns this straight into a timedelta.
    ("groceries", "keep_checked_hours"): lambda v: type(v) is int and 0 <= v <= engine.CHECKED_KEPT_HOURS_MAX,
}


@dataclass(frozen=True)
class Op:
    roles: tuple
    fn: object
    kinds: tuple = ()
    in_transaction: bool = True


OPS = {}


def op(name: str, roles: tuple, kinds: tuple = (), in_transaction: bool = True):
    def register(fn):
        OPS[name] = Op(roles, fn, kinds, in_transaction)
        return fn
    return register


def _text(args: dict, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise WallError(f"Missing '{name}'.")
    return value


def _day(args: dict, now):
    return engine.parse_day(args.get("day"), now.date())


def _kids(state: dict, args: dict) -> list:
    return engine.resolve(state, args["who"], kids_only=True) if args.get("who") else engine.kids(state)


def _settings(state: dict) -> dict:
    settings = state.get("settings")
    if not isinstance(settings, dict):
        settings = state["settings"] = {}
    return settings


# ── Parent PIN ──────────────────────────────────────────────────────────────

def _pin_set(state: dict) -> bool:
    stored = _settings(state).get("parent_pin")
    return isinstance(stored, str) and bool(stored)


def _check_pin(state: dict, pin, ctx) -> None:
    """A screen acting as a parent proves it with the parent PIN; wrong tries are rate-limited
    by the server's pin_limiter. Hashes written by this build (config.hash_pin) and by the
    earlier access.hash_pin both verify."""
    stored = _settings(state).get("parent_pin")
    if not _pin_set(state):
        raise WallError("Set a parent PIN from a phone first.")
    limiter = getattr(ctx, "pin_limiter", None)
    if limiter is not None and limiter.blocked():
        raise WallError("Too many wrong PINs. Try again in ten minutes.")
    if not (isinstance(pin, str) and (config.verify_pin(pin, stored) or access.verify_pin(pin, stored))):
        if limiter is not None:
            limiter.fail()
        raise WallError("That PIN isn't right.")


# ── Routines and chores ─────────────────────────────────────────────────────

def _tick(args, caller, now):
    """The day, member and item of a complete or uncomplete: the wall changes today's lists only."""
    day = _day(args, now)
    if caller.role == "kiosk" and day != now.date():
        raise WallError("The wall only changes today's lists.")
    return day, _text(args, "member"), _text(args, "item")


@op("complete", EVERYONE, kinds=("chores",))
def _complete(state, args, caller, now, ctx):
    day, member, item = _tick(args, caller, now)
    return engine.complete(state, member, item, day=day, actor=caller.actor, now=now)


@op("uncomplete", EVERYONE, kinds=("chores",))
def _uncomplete(state, args, caller, now, ctx):
    day, member, item = _tick(args, caller, now)
    return engine.uncomplete(state, member, item, day=day, actor=caller.actor, now=now)


@op("excuse", PARENTS, kinds=("chores",))
def _excuse(state, args, caller, now, ctx):
    day = _day(args, now)
    return {"excused": engine.excuse(state, args.get("who"), day=day), "day": day.isoformat()}


@op("assign_chore", PARENTS, kinds=("chores",))
def _assign_chore(state, args, caller, now, ctx):
    return engine.assign_chore(state, args.get("who"), args.get("title"), stars=args.get("stars", 1),
                               repeat=args.get("repeat") or "once", due=args.get("due"),
                               actor=caller.actor, now=now)


@op("remove_chore", PARENTS, kinds=("chores",))
def _remove_chore(state, args, caller, now, ctx):
    return engine.remove_chore(state, _text(args, "chore"), who=args.get("who"))


@op("chores", EVERYONE)
def _chores(state, args, caller, now, ctx):
    day = _day(args, now)
    return {kid["id"]: engine.kid_board(state, kid["id"], day) for kid in _kids(state, args)}


# ── Star Bank and rewards ───────────────────────────────────────────────────

@op("award_stars", PARENTS, kinds=("stars",))
def _award_stars(state, args, caller, now, ctx):
    return engine.award_stars(state, args.get("who"), args.get("amount"), args.get("reason") or "",
                              actor=caller.actor, now=now)


@op("stars", EVERYONE)
def _stars(state, args, caller, now, ctx):
    limit = args.get("history", 5)
    limit = limit if type(limit) is int and 0 <= limit <= 50 else 5
    return {kid["id"]: {"name": kid["name"], "balance": engine.balance(state, kid["id"]),
                        "available": engine.available(state, kid["id"]),
                        "history": engine.history(state, kid["id"], limit)}
            for kid in _kids(state, args)}


@op("request_reward", EVERYONE, kinds=("stars",))
def _request_reward(state, args, caller, now, ctx):
    return engine.request_reward(state, _text(args, "member"), _text(args, "reward"), actor=caller.actor, now=now)


def _decide(state, args, caller, now, ctx, approve: bool):
    """A parent's phone decides as itself; the kiosk decides as "parent at the wall" after the PIN."""
    actor = caller.actor
    if caller.role == "kiosk":
        _check_pin(state, args.get("pin"), ctx)
        actor = "parent at the wall"
    return engine.decide_reward(state, _text(args, "id"), approve, actor=actor, now=now)


@op("approve_reward", EVERYONE, kinds=("stars",))
def _approve_reward(state, args, caller, now, ctx):
    return _decide(state, args, caller, now, ctx, True)


@op("decline_reward", EVERYONE, kinds=("stars",))
def _decline_reward(state, args, caller, now, ctx):
    return _decide(state, args, caller, now, ctx, False)


@op("decide_reward", EVERYONE, kinds=("stars",))
def _decide_reward(state, args, caller, now, ctx):
    """approve_reward or decline_reward chosen by a boolean "approve", as the kiosk sends it."""
    approve = args.get("approve")
    if type(approve) is not bool:
        raise WallError("Approve or decline: true or false.")
    return _decide(state, args, caller, now, ctx, approve)


@op("add_reward", PARENTS, kinds=("rewards",))
def _add_reward(state, args, caller, now, ctx):
    return engine.add_reward(state, args.get("title"), args.get("stars", args.get("cost")),
                             args.get("emoji") or "🎁", actor=caller.actor)


@op("remove_reward", PARENTS, kinds=("rewards",))
def _remove_reward(state, args, caller, now, ctx):
    return engine.remove_reward(state, _text(args, "reward"))


@op("rewards", EVERYONE)
def _rewards(state, args, caller, now, ctx):
    return {"rewards": [{**r, "stars": engine.price(r)} for r in state["rewards"] if r.get("active", True)],
            "pending": [dict(r) for r in engine.redemptions(state) if r["status"] == "pending"]}


# ── Dinner, groceries and the family note ───────────────────────────────────

@op("set_meal", PARENTS, kinds=("meals",))
def _set_meal(state, args, caller, now, ctx):
    return engine.set_meal(state, _day(args, now), args.get("title"), args.get("notes") or "",
                           actor=caller.actor, now=now)


@op("clear_meal", PARENTS, kinds=("meals",))
def _clear_meal(state, args, caller, now, ctx):
    return engine.clear_meal(state, _day(args, now))


@op("meals", EVERYONE)
def _meals(state, args, caller, now, ctx):
    return {"week": engine.week_meals(state, now.date()), "ideas": engine.meal_ideas(state, now.date())}


@op("grocery_add", EVERYONE, kinds=("groceries",))
def _grocery_add(state, args, caller, now, ctx):
    return engine.add_groceries(state, args.get("items"), actor=caller.actor, now=now)


@op("grocery_check", EVERYONE, kinds=("groceries",))
def _grocery_check(state, args, caller, now, ctx):
    checked = args.get("checked", True)
    if type(checked) is not bool:
        raise WallError("'checked' is true or false.")
    return engine.check_groceries(state, args.get("items"), checked, actor=caller.actor, now=now)


@op("grocery_remove", PARENTS, kinds=("groceries",))
def _grocery_remove(state, args, caller, now, ctx):
    return {"removed": engine.remove_groceries(state, args.get("items"))}


@op("grocery_clear_checked", PARENTS, kinds=("groceries",))
def _grocery_clear_checked(state, args, caller, now, ctx):
    return {"cleared": engine.clear_checked(state)}


@op("groceries", EVERYONE)
def _groceries(state, args, caller, now, ctx):
    return {"items": engine.grocery_list(state)}


@op("note_set", PARENTS, kinds=("notes",))
def _note_set(state, args, caller, now, ctx):
    return engine.set_note(state, args.get("text"), actor=caller.actor, now=now)


@op("note", EVERYONE)
def _note(state, args, caller, now, ctx):
    return engine.note(state)


# The kiosk still sends the names an earlier build gave these ops.
OPS["add_groceries"] = OPS["grocery_add"]
OPS["check_groceries"] = OPS["grocery_check"]


# ── Settings ────────────────────────────────────────────────────────────────

def _refuse_protected(value, path: str = "settings") -> None:
    """No parent_pin anywhere in a settings write, however deeply it is nested."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in PROTECTED_SETTINGS:
                raise WallError("The parent PIN can't be changed through settings. Use set_pin.")
            _refuse_protected(inner, f"{path}.{key}")
    elif isinstance(value, list):
        for inner in value:
            _refuse_protected(inner, path)


def _check_setting_value(value, path: str, depth: int = 0) -> None:
    """Plain JSON only, small and shallow: the document is read by a browser, not a program."""
    if depth > SETTING_DEPTH_MAX:
        raise WallError(f"'{path}' is nested too deeply.")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if len(value) > SETTING_TEXT_MAX:
            raise WallError(f"Keep '{path}' to {SETTING_TEXT_MAX} characters.")
        return
    if isinstance(value, list):
        if len(value) > SETTING_LIST_MAX:
            raise WallError(f"Keep '{path}' to {SETTING_LIST_MAX} items.")
        for item in value:
            _check_setting_value(item, path, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not (isinstance(key, str) and SETTING_KEY.fullmatch(key)):
                raise WallError(f"'{key}' isn't a setting name.")
            _check_setting_value(item, f"{path}.{key}", depth + 1)
        return
    raise WallError(f"'{path}' must be text, a number, true/false, a list or an object.")


@op("settings_set", PARENTS, kinds=("settings",))
def _settings_set(state, args, caller, now, ctx):
    """Merge {"section": {"field": value, ...}} into the wall's settings, given directly as the
    args or under "settings". Only EDITABLE_SETTINGS sections may be written and the parent
    PIN never may: set_pin changes it, and only after checking the current one."""
    changes = args.get("settings", args)
    if not isinstance(changes, dict) or not changes:
        raise WallError('Say which settings to change, like {"night": {"start": "21:00"}}.')
    _refuse_protected(changes)
    settings = _settings(state)
    changed = {}
    for section, fields in changes.items():
        if section not in EDITABLE_SETTINGS:
            raise WallError(f"'{section}' isn't a setting a parent can change here. "
                            f"Sections: {', '.join(EDITABLE_SETTINGS)}.")
        if not isinstance(fields, dict) or not fields:
            raise WallError(f"Give '{section}' as an object of fields to change.")
        for key, value in fields.items():
            if not (isinstance(key, str) and SETTING_KEY.fullmatch(key)):
                raise WallError(f"'{key}' isn't a setting name.")
            _check_setting_value(value, f"{section}.{key}")
            rule = SETTING_RULES.get((section, key))
            if rule is not None and not rule(value):
                raise WallError(f"'{section}.{key}' can't be {value!r}.")
        target = settings.get(section)
        if not isinstance(target, dict):
            target = settings[section] = {}
        for key, value in fields.items():
            target[key] = deepcopy(value)
        changed[section] = deepcopy(target)
    return {"settings": changed}


@op("set_pin", PARENTS, kinds=("settings",))
def _set_pin(state, args, caller, now, ctx):
    """Change the parent PIN. While one is set, the current PIN ("current_pin") must be given;
    the new one is stored hashed and the sample-PIN reminder follows it."""
    pin = args.get("pin")
    if not (isinstance(pin, str) and config.PIN_PATTERN.fullmatch(pin)):
        raise WallError("The parent PIN is 4 to 8 digits.")
    if _pin_set(state):
        _check_pin(state, args.get("current_pin"), ctx)
    settings = _settings(state)
    settings["parent_pin"] = config.hash_pin(pin)
    settings["parent_pin_is_sample"] = pin == config.SAMPLE_PIN
    return {"pin_set": True, "pin_is_sample": settings["parent_pin_is_sample"]}


# ── Devices and status ──────────────────────────────────────────────────────

@op("pairing_code", PARENTS)
def _pairing_code(state, args, caller, now, ctx):
    role = args.get("role") or "parent"
    member = args.get("member")
    if role == "kiosk":
        # A wall panel may belong to one kid (or a parent), so a screen can be addressed by name.
        if member is not None and not (isinstance(member, str) and member in access.member_ids(state)):
            raise WallError("Unknown family member.")
    else:
        if member is not None and not isinstance(member, str):
            raise WallError("The paired member must be a parent id.")
        member = member or (caller.member if role == "parent" else None)
        if member is not None and member not in access.parent_ids(state):
            raise WallError("A phone is paired to a parent: " + ", ".join(sorted(access.parent_ids(state))) + ".")
    return ctx.pairing.issue(role, member, seconds=args.get("seconds"))


@op("revoke_device", PARENTS, kinds=("devices",))
def _revoke_device(state, args, caller, now, ctx):
    state.setdefault("devices", [])
    return access.revoke_device(state, _text(args, "device"))


@op("rename_device", PARENTS, kinds=("devices",))
def _rename_device(state, args, caller, now, ctx):
    state.setdefault("devices", [])
    extra = {"member": args["member"]} if "member" in args else {}
    return access.rename_device(state, _text(args, "device").strip(), name=args.get("name"), **extra)


@op("reload_browsers", PARENTS, in_transaction=False)
def _reload_browsers(store, args, caller, ctx):
    """Tell every connected screen to reload itself; nothing is written to the document."""
    revision = store.revision if store is not None else 0
    if ctx is not None and getattr(ctx, "bus", None) is not None:
        ctx.bus.publish({"revision": revision, "kinds": ["reload"]})
    return {"reloaded": True, "revision": revision}


@op("status", PARENTS)
def _status(state, args, caller, now, ctx):
    calendar = state.get("calendar") if isinstance(state.get("calendar"), dict) else {}
    photos = state.get("photos") if isinstance(state.get("photos"), dict) else {}
    return {"revision": state["revision"], "updated_at": state.get("updated_at"),
            "calendar": {"events": len(calendar.get("events") or []), "synced_at": calendar.get("synced_at"),
                         "error": calendar.get("error")},
            "photos": {"count": len(photos.get("items") or []), "synced_at": photos.get("synced_at"),
                       "error": photos.get("error")},
            "devices": [{"id": d["id"], "name": d["name"], "role": d["role"], "member": d.get("member")}
                        for d in state.get("devices") or [] if isinstance(d, dict) and not d.get("revoked")],
            "pin_set": _pin_set(state),
            "pin_is_sample": bool(_settings(state).get("parent_pin_is_sample"))}

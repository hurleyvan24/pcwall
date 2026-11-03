"""Routines and chores, streaks, the Star Bank, rewards, dinner, groceries and the family note.

Every mutating function works on the draft that Store.transact() hands it and raises
WallError with a sentence the family can act on; the store then drops the draft, so a
refused change never half-happens. Nothing here knows a particular family: the members,
routines and rewards are whatever config/family.json seeded into the document. Days are
local dates in the household time zone and `now` is an aware datetime.
"""

import re
import secrets
from collections import Counter
from datetime import date, datetime, timedelta

from pcwall import model


class WallError(ValueError):
    """A request the wall refuses. The message is shown to the family as it is."""


ALL_KIDS = {"all", "all kids", "kids", "the kids", "all the kids", "everyone", "everybody"}
TWINS = {"twins", "the twins", "both twins"}
COMPLETIONS_KEPT_DAYS = 35     # step-by-step history, and how far back a tick can be changed
ROUTINE_DAYS_KEPT = 400        # long enough for a year-long streak
LEDGER_DETAIL_DAYS = 120       # older star entries roll up into one carried-forward line per kid
MEALS_KEPT_DAYS = 90           # dinner history feeds the "again?" suggestions
CHECKED_KEPT_HOURS = 24        # ticked groceries fall off the list after this unless cleared sooner
CHECKED_KEPT_HOURS_MAX = 720   # the longest a parent may keep them, 30 days; ops checks the setting against it
MAX_TITLE = 80
MAX_NOTE = 280
MAX_REWARD_COST = 5000         # the biggest prize a parent can put in the store
DAILY_BONUS = 2                # for ticking every routine step and chore of the day
DAILY_BONUS_TITLE = "Completion / On-Task Bonus"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(3)}"


def _text(value, what: str, limit: int, *, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise WallError(f"The {what} must be text.")
    text = " ".join(value.split())
    if required and not text:
        raise WallError(f"The {what} can't be empty.")
    if len(text) > limit:
        raise WallError(f"Keep the {what} to {limit} characters.")
    return text


def _names(members) -> str:
    names = [m["name"] for m in members]
    if not names:
        return "nobody yet"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def parse_day(value, today: date) -> date:
    """A date, 'today'/'tonight', 'tomorrow', 'yesterday', a weekday name or prefix
    (the next one, today included), or an ISO date."""
    if isinstance(value, date):
        return value
    text = model.normalize(value or "today").removesuffix(" night")
    if text in ("today", "tonight"):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    if text == "yesterday":
        return today - timedelta(days=1)
    for index, name in enumerate(model.DAY_NAMES):
        if len(text) >= 3 and name.startswith(text):
            return today + timedelta(days=(index - today.weekday()) % 7)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise WallError("Use a date like 2026-10-14, 'today', 'tomorrow' or a weekday.") from None


# ── Where the document keeps things ───────────────────────────────────────
#
# A document seeded by config.default_document keeps the Star Bank under "stars"
# ({"balances": {...}, "ledger": [...]}) and reward requests under "reward_requests";
# one written by an earlier build has a top-level "ledger" and "redemptions", and
# neither shape is guaranteed to carry "chores", "routine_days" or "excused" until
# something is written there. Everything below reads and writes through these
# accessors, so both shapes work. A read never adds a key: views work on the store's
# live state, and the store decides whether to save by comparing the draft to it.

def _part(state: dict, key: str, kind, *, create: bool = False):
    """state[key] when it is a `kind` (list or dict); otherwise an empty one, stored in the
    document only when `create` is set."""
    value = state.get(key)
    if isinstance(value, kind):
        return value
    if not create:
        return kind()
    value = state[key] = kind()
    return value


def _ledgers(state: dict) -> tuple:
    """The two places a star ledger can live, each a list or None: state["ledger"] on an
    older document and state["stars"]["ledger"] on one seeded by this build."""
    top = state.get("ledger")
    stars = state.get("stars")
    nested = stars.get("ledger") if isinstance(stars, dict) else None
    return (top if isinstance(top, list) else None), (nested if isinstance(nested, list) else None)


def ledger(state: dict, *, create: bool = False) -> list:
    """The star ledger: state["ledger"] on an older document, else state["stars"]["ledger"].
    A document carrying both uses the one with entries in it, so an empty top-level list left
    behind by an earlier build never hides the stars kept in stars.ledger and the accounting
    is never split between the two. When both hold entries the older top-level list still
    wins, as before; when both are empty the stars.ledger this build writes is the active one."""
    top, nested = _ledgers(state)
    if top:
        return top
    if nested is not None:
        return nested
    if top is not None:
        return top
    if not create:
        return []
    stars = _part(state, "stars", dict, create=True)
    stars["ledger"] = []
    return stars["ledger"]


def _replace_ledger(state: dict, entries: list) -> None:
    """Swap the active ledger, the list ledger() returns, for `entries`, so a roll-up lands
    where the stars are. Purge the secondary ledger so it cannot revive if entries is empty."""
    top, _ = _ledgers(state)
    if top is not None and ledger(state) is top:
        state["ledger"] = entries
        if isinstance(state.get("stars"), dict):
            state["stars"].pop("ledger", None)
    else:
        _part(state, "stars", dict, create=True)["ledger"] = entries
        state.pop("ledger", None)


def redemptions(state: dict, *, create: bool = False) -> list:
    """Reward requests: state["redemptions"] on an older document, else state["reward_requests"]."""
    if isinstance(state.get("redemptions"), list):
        return state["redemptions"]
    return _part(state, "reward_requests", list, create=create)


def chores(state: dict, *, create: bool = False) -> list:
    return _part(state, "chores", list, create=create)


def completions(state: dict, *, create: bool = False) -> dict:
    return _part(state, "completions", dict, create=create)


def routine_days(state: dict, *, create: bool = False) -> dict:
    return _part(state, "routine_days", dict, create=create)


def excused(state: dict, *, create: bool = False) -> dict:
    return _part(state, "excused", dict, create=create)


def groceries(state: dict, *, create: bool = False) -> list:
    return _part(state, "groceries", list, create=create)


def meals(state: dict, *, create: bool = False) -> dict:
    return _part(state, "meals", dict, create=create)


def price(reward: dict) -> int:
    """What a reward costs: "stars", as family.json names it, or "cost" on an older document."""
    value = reward.get("stars", reward.get("cost", 0))
    return value if type(value) is int else 0


def _settings(state: dict, section: str) -> dict:
    settings = state.get("settings")
    value = settings.get(section) if isinstance(settings, dict) else None
    return value if isinstance(value, dict) else {}


# ── Members ───────────────────────────────────────────────────────────────

def kids(state: dict) -> list:
    return [m for m in state["members"] if m["role"] == "kid"]


def member(state: dict, member_id: str) -> dict:
    for candidate in state["members"]:
        if candidate["id"] == member_id:
            return candidate
    raise WallError(f"There is no '{member_id}' on the wall.")


def kid(state: dict, member_id: str) -> dict:
    found = member(state, member_id)
    if found["role"] != "kid":
        raise WallError(f"Only the kids have routines and stars: {_names(kids(state))}.")
    return found


def resolve(state: dict, who, *, kids_only: bool = False) -> list:
    """A name, id or alias, a list of them ('Riley, Taylor', 'sam and riley'), 'the twins'
    or 'all kids' -> member dicts, in the order given."""
    if isinstance(who, (list, tuple)):
        who = ", ".join(map(str, who))
    text = model.normalize(who)
    if not text:
        raise WallError(f"Say who: {_names(kids(state))}.")
    if text in ALL_KIDS and (kids_only or text not in ("everyone", "everybody")):
        return kids(state)
    if text in TWINS:
        return [m for m in kids(state) if m.get("twin")]
    found = []
    for part in re.split(r"\s*(?:,|&|\+|\band\b)\s*", text):
        if not part:
            continue
        match = next((m for m in state["members"] if part in
                      {m["id"], model.normalize(m["name"]), *map(model.normalize, m.get("aliases") or [])}), None)
        if match is None:
            raise WallError(f"I don't know '{part}'. The wall has {_names(state['members'])}.")
        if kids_only and match["role"] != "kid":
            raise WallError(f"Only the kids have chores and stars: {_names(kids(state))}.")
        if match not in found:
            found.append(match)
    return found


# ── Routines and chores ───────────────────────────────────────────────────

def routines_for(state: dict, member_id: str, day: date) -> list:
    weekday = model.DAYS[day.weekday()]
    return [r for r in state["routines"]
            if r["member"] == member_id and r.get("active", True) and weekday in r["days"]]


def chore_due(chore: dict, day: date) -> bool:
    stamp = day.isoformat()
    if not chore.get("active", True) or stamp < chore["start"]:
        return False
    repeat = chore["repeat"]
    if repeat == "once":
        done_on = chore.get("done_on")
        return chore["due"] <= stamp and (done_on is None or stamp == done_on)
    if repeat == "daily":
        return True
    if repeat == "weekdays":
        return day.weekday() < 5
    if repeat == "weekends":
        return day.weekday() >= 5
    return model.DAYS[day.weekday()] in repeat.removeprefix("weekly:").split(",")


def _item(state: dict, member_id: str, item: str, day: date):
    """'step:<routine id>:<step id>' or 'chore:<chore id>' -> (kind, routine, step or chore)."""
    kind, _, rest = str(item).partition(":")
    if kind == "step":
        routine_id, _, step_id = rest.rpartition(":")
        routine = next((r for r in routines_for(state, member_id, day) if r["id"] == routine_id), None)
        step = next((s for s in (routine or {}).get("steps", []) if s["id"] == step_id), None)
        if step is None:
            raise WallError("That step isn't on this day's list.")
        return "step", routine, step
    if kind == "chore":
        chore = next((c for c in chores(state) if c["id"] == rest and c["member"] == member_id), None)
        if chore is None or not chore_due(chore, day):
            raise WallError("That chore isn't on this day's list.")
        return "chore", None, chore
    raise WallError(f"Unknown item '{item}'.")


def _done(state: dict, member_id: str, day: date) -> dict:
    return completions(state).get(day.isoformat(), {}).get(member_id, {})


def _step_items(routine: dict) -> list:
    return [f"step:{routine['id']}:{s['id']}" for s in routine["steps"]]


def _routine_finished(routine: dict, done: dict) -> bool:
    """Every step of the routine is in `done`."""
    items = _step_items(routine)
    return bool(items) and all(item in done for item in items)


def _all_day_done(state: dict, member_id: str, day: date) -> bool:
    """Every routine step scheduled for `day` and every chore due that day is ticked, and
    there was at least one of them: a day with nothing on the list earns no bonus."""
    done = _done(state, member_id, day)
    items = [item for r in routines_for(state, member_id, day) for item in _step_items(r)]
    items += [f"chore:{c['id']}" for c in chores(state) if c["member"] == member_id and chore_due(c, day)]
    return bool(items) and all(item in done for item in items)


def _changeable(day: date, now) -> None:
    """A tick older than the step history (COMPLETIONS_KEPT_DAYS) could be paid twice
    once its record is gone, and a future day hasn't happened yet."""
    age = (now.date() - day).days
    if age > COMPLETIONS_KEPT_DAYS:
        raise WallError("That day is too long ago to change.")
    if age < 0:
        raise WallError("That day hasn't happened yet.")


def complete(state: dict, member_id: str, item: str, *, day: date, actor: str, now) -> dict:
    """Tick one step or chore. A second tap changes nothing, so a double tap pays once."""
    kid(state, member_id)
    _changeable(day, now)
    kind, routine, thing = _item(state, member_id, item, day)
    stamp = day.isoformat()
    done = completions(state, create=True).setdefault(stamp, {}).setdefault(member_id, {})
    result = {"member": member_id, "item": item, "day": stamp, "already": item in done, "stars": 0,
              "routine_done": False, "streak": None, "daily_bonus": 0}
    if result["already"]:
        return result
    if kind == "chore" and thing["repeat"] == "once":
        if thing.get("done_on") is not None and thing["done_on"] != stamp:
            raise WallError("That one-off chore has already been completed.")
        thing["done_on"] = stamp
        ref = item
    else:
        ref = f"{item}@{stamp}"
    done[item] = {"at": now.isoformat(), "by": actor}
    stars = thing.get("stars", 1)
    result["stars"] = _post_once(state, member_id, stars if type(stars) is int else 1, kind, thing["title"],
                                 ref, actor, now)
    if kind == "step" and _routine_finished(routine, done):
        result.update(_finish_routine(state, routine, day))
    if _all_day_done(state, member_id, day):   # the last tick of the day, whichever list it was on
        result["daily_bonus"] = _post_once(state, member_id, DAILY_BONUS, "daily_bonus",
                                           f"{DAILY_BONUS_TITLE} (All chores done)",
                                           f"daily_bonus:{member_id}@{stamp}", actor, now)
    return result


def _finish_routine(state: dict, routine: dict, day: date) -> dict:
    """Record the day as finished for the routine's streak. No stars change hands here: each
    step paid itself, and the only bonus on the wall is the day's Completion / On-Task Bonus
    (see complete()). A routine's "bonus" field and the streak_milestones setting are never
    paid, so a document that still carries them earns nothing extra."""
    stamp = day.isoformat()
    finished = routine_days(state, create=True).setdefault(routine["id"], [])
    if stamp not in finished:
        finished.append(stamp)
        finished.sort()
    return {"routine_done": True, "streak": streak(state, routine, day)}


def uncomplete(state: dict, member_id: str, item: str, *, day: date, actor: str, now) -> dict:
    """Untick a step or chore and take back exactly what ticking it paid."""
    kid(state, member_id)
    _changeable(day, now)
    stamp = day.isoformat()
    done = _done(state, member_id, day)
    if item not in done:
        return {"member": member_id, "item": item, "day": stamp, "already": True, "stars": 0}
    del done[item]
    kind, _, rest = str(item).partition(":")
    taken = _reverse(state, f"{item}@{stamp}", actor, now)
    if kind == "step":
        routine_id = rest.rpartition(":")[0]
        finished = routine_days(state).get(routine_id, [])
        routine = next((r for r in state["routines"] if r["id"] == routine_id), None)
        if stamp in finished and not (routine is not None and _routine_finished(routine, done)):
            finished.remove(stamp)
            # Older builds paid a routine bonus and streak milestones; a document that still
            # carries those payments gives them back with the day.
            taken += _reverse(state, f"routine:{routine_id}@{stamp}", actor, now)
            taken += _reverse(state, f"streak:{routine_id}@{stamp}", actor, now)
    else:
        chore = next((c for c in chores(state) if c["id"] == rest), None)
        if chore is not None and chore.get("done_on") == stamp:
            chore.pop("done_on")
    if not _all_day_done(state, member_id, day):   # the day is no longer complete, so its bonus goes back too
        taken += _reverse(state, f"daily_bonus:{member_id}@{stamp}", actor, now)
    if balance(state, member_id) < 0:
        raise WallError(f"Those stars are already spent, so {member(state, member_id)['name']} "
                        "can't untick this. A parent can fix it.")
    return {"member": member_id, "item": item, "day": stamp, "already": False, "stars": -taken}


def streak(state: dict, routine: dict, day: date) -> int:
    """Scheduled days in a row, up to and including `day`, on which the routine was
    finished. Excused days and days off the routine's schedule neither break nor extend it."""
    finished = set(routine_days(state).get(routine["id"], []))
    days_off = set(excused(state).get(routine["member"], []))
    count, cursor = 0, day
    for _ in range(ROUTINE_DAYS_KEPT):
        if model.DAYS[cursor.weekday()] in routine["days"]:
            stamp = cursor.isoformat()
            if stamp in days_off:
                pass
            elif stamp in finished:
                count += 1
            else:
                break
        cursor -= timedelta(days=1)
    return count


def current_streak(state: dict, routine: dict, today: date) -> int:
    """The streak the wall shows: an unfinished routine today doesn't break it yet."""
    if today.isoformat() in routine_days(state).get(routine["id"], []):
        return streak(state, routine, today)
    return streak(state, routine, today - timedelta(days=1))


def excuse(state: dict, who, *, day: date) -> list:
    """Sick day or vacation: the day keeps every routine streak alive for these kids."""
    names = []
    for found in resolve(state, who, kids_only=True):
        days = excused(state, create=True).setdefault(found["id"], [])
        if day.isoformat() not in days:
            days.append(day.isoformat())
            days.sort()
        names.append(found["name"])
    return names


REPEATS = {"once": "once", "one time": "once", "today": "once", "daily": "daily",
           "every day": "daily", "everyday": "daily", "weekdays": "weekdays",
           "school days": "weekdays", "every weekday": "weekdays", "weekends": "weekends",
           "every weekend": "weekends"}


def parse_repeat(value) -> str:
    """'once', 'daily', 'weekdays', 'weekends' or days: 'saturdays', 'mon, wed', 'weekly:tue,thu'."""
    text = model.normalize(value or "once")
    if text in REPEATS:
        return REPEATS[text]
    text = text.removeprefix("weekly:").removeprefix("weekly on ").removeprefix("every ")
    days = []
    for part in re.split(r"\s*(?:,|&|/|\band\b)\s*", text):
        part = part[:-1] if part.endswith("days") else part
        day = next((code for code, name in zip(model.DAYS, model.DAY_NAMES)
                    if len(part) >= 2 and name.startswith(part)), None)
        if day is None:
            raise WallError("Say how often: once, daily, weekdays, weekends, or days like 'mon, wed'.")
        if day not in days:
            days.append(day)
    return "weekly:" + ",".join(sorted(days, key=model.DAYS.index))


def assign_chore(state: dict, who, title, *, stars=1, repeat="once", due=None, actor: str, now) -> list:
    title = _text(title, "chore", MAX_TITLE)
    if type(stars) is not int or not 1 <= stars <= 20:
        raise WallError("A chore is worth 1 to 20 stars.")
    rule = parse_repeat(repeat)
    today = now.date()
    due_day = parse_day(due, today) if rule == "once" else None
    assigned = []
    for found in resolve(state, who, kids_only=True):
        pool = chores(state, create=True)
        chore = next((c for c in pool if c["member"] == found["id"] and c.get("active", True)
                      and model.normalize(c["title"]) == model.normalize(title)), None)
        if chore is None:
            chore = {"id": new_id("ch"), "member": found["id"], "title": title, "created_by": actor,
                     "created_at": now.isoformat(), "start": today.isoformat(), "active": True}
            pool.append(chore)
        chore.update(stars=stars, repeat=rule, due=due_day.isoformat() if due_day else None)
        chore.pop("done_on", None)
        assigned.append(dict(chore))
    return assigned


def remove_chore(state: dict, chore, who=None) -> list:
    text = model.normalize(chore)
    pool = [c for c in chores(state) if c.get("active", True)]
    if who:
        ids = {found["id"] for found in resolve(state, who, kids_only=True)}
        pool = [c for c in pool if c["member"] in ids]
    hits = [c for c in pool if text in (c["id"], model.normalize(c["title"]))]
    if not hits:
        raise WallError(f"No chore called '{chore}'.")
    for hit in hits:
        hit["active"] = False
    return [dict(hit) for hit in hits]


# ── Star Bank ledger ──────────────────────────────────────────────────────

def post(state: dict, member_id: str, delta: int, kind: str, reason: str, *, actor: str, now, ref=None) -> dict:
    """Append one ledger entry. The ledger is the balance; a document that also keeps
    stars.balances has that running total moved along so the raw file reads right."""
    entry = {"id": new_id("tx"), "at": now.isoformat(), "member": member_id, "delta": int(delta),
             "kind": kind, "reason": reason, "by": actor}
    if ref:
        entry["ref"] = ref
    ledger(state, create=True).append(entry)
    balances = state["stars"].get("balances") if isinstance(state.get("stars"), dict) else None
    if isinstance(balances, dict):
        balances[member_id] = balances.get(member_id, 0) + entry["delta"]
    return entry


def net(state: dict, ref: str) -> int:
    return sum(e["delta"] for e in ledger(state) if e.get("ref") == ref)


def _post_once(state, member_id, stars, kind, reason, ref, actor, now) -> int:
    """Pay `stars` for `ref` unless it is already paid; an undone payment can be paid again."""
    if not stars or net(state, ref) != 0:
        return 0
    post(state, member_id, stars, kind, reason, actor=actor, now=now, ref=ref)
    return stars


def _reverse(state, ref, actor, now) -> int:
    paid = net(state, ref)
    if paid:
        owner = next(e["member"] for e in ledger(state) if e.get("ref") == ref)
        post(state, owner, -paid, "undo", "Unticked", actor=actor, now=now, ref=ref)
    return paid


def balance(state: dict, member_id: str) -> int:
    return sum(e["delta"] for e in ledger(state) if e["member"] == member_id)


def held(state: dict, member_id: str) -> int:
    return sum(r["cost"] for r in redemptions(state) if r["member"] == member_id and r["status"] == "pending")


def available(state: dict, member_id: str) -> int:
    return balance(state, member_id) - held(state, member_id)


# ── A kid's column on the wall ────────────────────────────────────────────

def kid_board(state: dict, member_id: str, day: date) -> dict:
    """One kid's column on the wall for `day`."""
    done = _done(state, member_id, day)
    routines = []
    for routine in routines_for(state, member_id, day):
        steps = [{"id": s["id"], "title": s["title"], "emoji": s.get("emoji"), "stars": s.get("stars", 1),
                  "item": f"step:{routine['id']}:{s['id']}",
                  "done": f"step:{routine['id']}:{s['id']}" in done} for s in routine["steps"]]
        finished = sum(s["done"] for s in steps)
        # "routine" is the family.json routine id (morning, bedtime); "kind" is its older name.
        kind = routine.get("routine") or routine.get("kind")
        routines.append({"id": routine["id"], "routine": kind, "kind": kind, "title": routine["title"],
                         "emoji": routine.get("emoji"), "start": routine["start"], "end": routine["end"],
                         "steps": steps, "done": finished, "total": len(steps),
                         "complete": bool(steps) and finished == len(steps),
                         "streak": current_streak(state, routine, day)})
    due = [{"id": c["id"], "title": c["title"], "stars": c["stars"], "repeat": c["repeat"],
            "item": f"chore:{c['id']}", "done": f"chore:{c['id']}" in done}
           for c in chores(state) if c["member"] == member_id and chore_due(c, day)]
    return {"member": member_id, "routines": routines, "chores": due,
            "daily_bonus": {"title": DAILY_BONUS_TITLE, "stars": DAILY_BONUS,
                            "done": _all_day_done(state, member_id, day)},
            "stars": balance(state, member_id), "available": available(state, member_id),
            "pending": [dict(r) for r in redemptions(state)
                        if r["member"] == member_id and r["status"] == "pending"]}


# ── Awards and history ────────────────────────────────────────────────────

def award_stars(state: dict, who, amount, reason="", *, actor: str, now) -> dict:
    """Bonus stars (positive) or stars taken away (negative); a balance may go below zero."""
    if type(amount) is not int or amount == 0:
        raise WallError("Give a whole number of stars, like 2 or -1.")
    limit = _settings(state, "stars").get("max_award", 5)
    if abs(amount) > limit:
        raise WallError(f"Keep it to {limit} stars at a time.")
    reason = _text(reason, "reason", 120, required=False) or ("Bonus stars" if amount > 0 else "Stars taken away")
    found = resolve(state, who, kids_only=True)
    for each in found:
        post(state, each["id"], amount, "award" if amount > 0 else "deduct", reason, actor=actor, now=now)
    return {"members": [m["id"] for m in found], "amount": amount, "reason": reason,
            "balances": {m["id"]: balance(state, m["id"]) for m in found}}


def history(state: dict, member_id: str, limit: int = 10) -> list:
    """The newest `limit` entries first (limit 0 means none, not all)."""
    return [e for e in ledger(state) if e["member"] == member_id][::-1][:limit]


# ── Rewards ───────────────────────────────────────────────────────────────

def _pick_reward(text: str, candidates: list):
    """The candidate whose id or title is `text`, else the only one whose title contains it."""
    for candidate in candidates:
        if text in (candidate["id"], model.normalize(candidate["title"])):
            return candidate
    hits = [r for r in candidates if text and text in model.normalize(r["title"])]
    return hits[0] if len(hits) == 1 else None


def reward(state: dict, ref) -> dict:
    """The active catalog entry `ref` names by id or title. The catalog is the family's own,
    seeded from family.json and edited on the wall; there is no built-in list to fall back on."""
    text = model.normalize(ref)
    active = [r for r in state["rewards"] if r.get("active", True)]
    prize = _pick_reward(text, active)
    if prize is None:
        raise WallError(f"No reward matches '{ref}'. Rewards: " + ", ".join(r["title"] for r in active) + ".")
    return prize


def add_reward(state: dict, title, stars, emoji="🎁", *, actor: str) -> dict:
    title = _text(title, "reward", MAX_TITLE)
    if type(stars) is not int or not 1 <= stars <= MAX_REWARD_COST:
        raise WallError(f"A reward costs 1 to {MAX_REWARD_COST} stars.")
    if any(model.normalize(r["title"]) == model.normalize(title) for r in state["rewards"] if r.get("active", True)):
        raise WallError(f"'{title}' is already in the catalog.")
    prize = {"id": new_id("rw"), "title": title, "emoji": _text(emoji, "emoji", 8, required=False) or "🎁",
             "stars": stars, "active": True, "created_by": actor}
    state["rewards"].append(prize)
    return dict(prize)


def remove_reward(state: dict, ref) -> dict:
    prize = reward(state, ref)
    prize["active"] = False
    return dict(prize)


def request_reward(state: dict, member_id: str, ref, *, actor: str, now) -> dict:
    """A kid asks; the stars are held until a parent approves or declines."""
    asker = kid(state, member_id)
    prize = reward(state, ref)
    for request in redemptions(state):
        if request["member"] == member_id and request["reward"] == prize["id"] and request["status"] == "pending":
            return {**request, "already": True}
    cost = price(prize)
    short = cost - available(state, member_id)
    if short > 0:
        raise WallError(f"{asker['name']} needs {short} more stars for {prize['title']}.")
    request = {"id": new_id("rd"), "member": member_id, "reward": prize["id"], "title": prize["title"],
               "emoji": prize.get("emoji") or "🎁", "cost": cost, "status": "pending",
               "requested_at": now.isoformat(), "requested_by": actor}
    redemptions(state, create=True).append(request)
    return {**request, "already": False}


def decide_reward(state: dict, request_id: str, approve, *, actor: str, now) -> dict:
    if type(approve) is not bool:
        raise WallError("Approve or decline: true or false.")
    request = next((r for r in redemptions(state) if r["id"] == request_id), None)
    if request is None:
        raise WallError("That reward request no longer exists.")
    if request["status"] != "pending":
        return {**request, "already": True}
    if approve:
        if balance(state, request["member"]) < request["cost"]:
            raise WallError(f"{member(state, request['member'])['name']} no longer has {request['cost']} stars.")
        post(state, request["member"], -request["cost"], "redeem", f"{request['emoji']} {request['title']}",
             actor=actor, now=now, ref=f"redeem:{request['id']}")
    request.update(status="approved" if approve else "declined", decided_by=actor, decided_at=now.isoformat())
    return {**request, "already": False}


def approve_reward(state: dict, request_id: str, *, actor: str, now) -> dict:
    return decide_reward(state, request_id, True, actor=actor, now=now)


def decline_reward(state: dict, request_id: str, *, actor: str, now) -> dict:
    return decide_reward(state, request_id, False, actor=actor, now=now)


# ── Dinner ────────────────────────────────────────────────────────────────

def day_label(day: date, today: date) -> str:
    offset = (day - today).days
    if offset == 0:
        return "Tonight"
    if offset == 1:
        return "Tomorrow"
    if 1 < offset < 7:
        return f"{day:%A}"
    return f"{day:%a} {day:%b} {day.day}"


def set_meal(state: dict, day: date, title, notes="", *, actor: str, now) -> dict:
    meal = {"title": _text(title, "dinner", MAX_TITLE), "notes": _text(notes, "notes", 300, required=False),
            "by": actor, "at": now.isoformat()}
    meals(state, create=True)[day.isoformat()] = meal
    return {"day": day.isoformat(), **meal}


def clear_meal(state: dict, day: date) -> dict:
    meal = meals(state).pop(day.isoformat(), None)
    if meal is None:
        raise WallError(f"No dinner is planned for {day:%A}, {day:%b} {day.day}.")
    return {"day": day.isoformat(), **meal}


def week_meals(state: dict, today: date, days: int = 7) -> list:
    planned = meals(state)
    week = (today + timedelta(days=n) for n in range(days))
    return [{"day": d.isoformat(), "label": day_label(d, today), "meal": planned.get(d.isoformat())}
            for d in week]


def meal_ideas(state: dict, today: date, limit: int = 8) -> list:
    """Dinners from the last 90 days, most often first: one tap to plan them again."""
    since = (today - timedelta(days=MEALS_KEPT_DAYS)).isoformat()
    counts = Counter(meal["title"] for day, meal in sorted(meals(state).items()) if since <= day < today.isoformat())
    return [title for title, _ in counts.most_common(limit)]


# ── Groceries ─────────────────────────────────────────────────────────────

_QUANTITY = re.compile(r"^(?:(\d+(?:\.\d+)?)(?:\s*x\s+|\s+)(.+)|(.+?)\s+x\s*(\d+))$", re.I)


def _split_quantity(text: str):
    match = _QUANTITY.match(text)
    if not match:
        return text, None
    if match.group(1):
        return match.group(2).strip(), match.group(1)
    return match.group(3).strip(), match.group(4)


def _same(a: str, b: str) -> bool:
    a, b = model.normalize(a), model.normalize(b)
    return a == b or model.singular(a) == model.singular(b)


def _items(items) -> list:
    if isinstance(items, str):
        items = re.split(r"\s*[,;\n]\s*", items)
    if not isinstance(items, list):
        raise WallError("Give the items as a list.")
    return [i for i in items if isinstance(i, str) and i.strip()]


def add_groceries(state: dict, items, *, actor: str, now) -> dict:
    added, already, back = [], [], []
    pool = groceries(state, create=True)
    for raw in _items(items):
        name, quantity = _split_quantity(_text(raw, "item", 60))
        match = next((g for g in pool if _same(g["name"], name)), None)
        if match is None:
            pool.append({"id": new_id("gr"), "name": name, "qty": quantity,
                         "aisle": model.aisle_for(name), "checked": False,
                         "added_by": actor, "added_at": now.isoformat()})
            added.append(name)
            continue
        if quantity:
            match["qty"] = quantity
        if match["checked"]:
            match["checked"] = False
            match.pop("checked_by", None)
            match.pop("checked_at", None)
            back.append(match["name"])
        else:
            already.append(match["name"])
    if not (added or already or back):
        raise WallError("Say what to add, like 'milk, eggs, bananas'.")
    return {"added": added, "already": already, "back": back}


def _find_grocery(pool: list, ref: str):
    exact = next((g for g in pool if g["id"] == ref or _same(g["name"], ref)), None)
    if exact is not None:
        return exact
    starts = [g for g in pool if model.normalize(g["name"]).startswith(model.normalize(ref))]
    return starts[0] if len(starts) == 1 else None


def check_groceries(state: dict, items, checked=True, *, actor: str, now) -> dict:
    """Tick (or untick) items by id, name, plural or unique prefix."""
    changed, already, missing = [], [], []
    pool = groceries(state)
    for ref in _items(items):
        target = _find_grocery([g for g in pool if g["checked"] != checked], ref)
        if target is None:
            done = _find_grocery([g for g in pool if g["checked"] == checked], ref)
            (already if done else missing).append(done["name"] if done else ref)
            continue
        target["checked"] = checked
        if checked:
            target.update(checked_by=actor, checked_at=now.isoformat())
        else:
            target.pop("checked_by", None)
            target.pop("checked_at", None)
        changed.append(target["name"])
    if not changed and not already:
        raise WallError("Nothing on the list matches " + ", ".join(f"'{m}'" for m in missing) + ".")
    return {"changed": changed, "already": already, "missing": missing}


def remove_groceries(state: dict, items) -> list:
    removed = []
    pool = groceries(state)
    for ref in _items(items):
        target = _find_grocery(pool, ref)
        if target is not None:
            pool.remove(target)
            removed.append(target["name"])
    if not removed:
        raise WallError("Nothing on the list matches that.")
    return removed


def clear_checked(state: dict) -> int:
    pool = groceries(state)
    before = len(pool)
    pool[:] = [g for g in pool if not g["checked"]]
    return before - len(pool)


def grocery_list(state: dict) -> list:
    """Unticked first, in store-aisle order; ticked ones last until they are cleared."""
    order = {name: index for index, name in enumerate(model.AISLE_ORDER)}
    return sorted((dict(g) for g in groceries(state)),
                  key=lambda g: (g["checked"], order.get(g["aisle"], len(order)), g["added_at"]))


# ── The family note ───────────────────────────────────────────────────────

def note(state: dict) -> dict:
    """The one note the wall shows: {"text", "updated_at", "by"}. A document from an older
    build kept a list of dated notes under the same key; that reads as no note."""
    value = state.get("notes")
    if isinstance(value, dict):
        return {"text": str(value.get("text") or ""), "updated_at": value.get("updated_at"),
                "by": value.get("by")}
    return {"text": "", "updated_at": None, "by": None}


def set_note(state: dict, text, *, actor: str, now) -> dict:
    """Write the family note; empty text clears it."""
    state["notes"] = {"text": _text(text, "note", MAX_NOTE, required=False),
                      "updated_at": now.isoformat(), "by": actor}
    return dict(state["notes"])


# ── Housekeeping ──────────────────────────────────────────────────────────

def keep_checked_hours(state: dict) -> int:
    """How long a ticked grocery stays on the list: groceries.keep_checked_hours from the
    settings when it is a nonnegative whole number of hours, else CHECKED_KEPT_HOURS.
    Stored values may exceed the cap ops applies to new settings; a bad value that reached
    the document some other way must not stop the daily tidy-up."""
    hours = _settings(state, "groceries").get("keep_checked_hours")
    if type(hours) is int and hours >= 0:
        return hours
    return CHECKED_KEPT_HOURS


def maintain(state: dict, now) -> dict:
    """Daily tidy-up: old history out, finished one-off chores retired. Returns counts."""
    today = now.date()
    stamp = today.isoformat()
    counts = Counter()
    oldest = (today - timedelta(days=COMPLETIONS_KEPT_DAYS)).isoformat()
    done = completions(state)
    for day in [d for d in done if d < oldest]:
        del done[day]
        counts["completions"] += 1
    keep_from = (today - timedelta(days=ROUTINE_DAYS_KEPT)).isoformat()
    for days in list(routine_days(state).values()) + list(excused(state).values()):
        days[:] = [d for d in days if d >= keep_from]
    meals_from = (today - timedelta(days=MEALS_KEPT_DAYS)).isoformat()
    planned = meals(state)
    for day in [d for d in planned if d < meals_from]:
        del planned[day]
        counts["meals"] += 1
    stale = now - timedelta(hours=keep_checked_hours(state))
    pool = groceries(state)
    before = len(pool)
    pool[:] = [g for g in pool if not (g["checked"] and datetime.fromisoformat(g["checked_at"]) < stale)]
    counts["groceries"] = before - len(pool)
    for chore in chores(state):
        if chore["repeat"] == "once" and chore.get("active", True) and chore.get("done_on", stamp) < stamp:
            chore["active"] = False
            counts["chores"] += 1
    return dict(+counts)


def compact_ledger(state: dict, now) -> list:
    """Roll star entries older than LEDGER_DETAIL_DAYS into one carried-forward entry per
    kid, so every balance stays the same and the file stays small (a tap's save time grows
    with it). Returns the rolled-up entries for the store's archive; earlier carried-forward
    lines are folded in but not returned, since they only summarize archived ones."""
    cutoff = now - timedelta(days=LEDGER_DETAIL_DAYS)
    entries = ledger(state)
    rolled = [e for e in entries if datetime.fromisoformat(e["at"]) < cutoff]
    if not any(e["kind"] != "carried" for e in rolled):
        return []
    totals = Counter()
    for entry in rolled:
        totals[entry["member"]] += entry["delta"]
    carried = [{"id": new_id("tx"), "at": cutoff.isoformat(), "member": member_id, "delta": total,
                "kind": "carried", "reason": "Earlier stars, carried forward", "by": "wall"}
               for member_id, total in totals.items() if total]
    _replace_ledger(state, carried + [e for e in entries if datetime.fromisoformat(e["at"]) >= cutoff])
    return [e for e in rolled if e["kind"] != "carried"]


complete_chore = complete

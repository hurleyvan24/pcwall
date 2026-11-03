"""GET /api/state: everything a wall or a phone draws, already worked out.

The browser only renders; ages, grades, streaks, balances, aisle order and day labels are
computed here from the store's document and the household's school rules, so the panel's
slow CPU never runs business logic. The document that leaves here never carries the parent
PIN, hashed or not: a screen learns only whether one is set and whether it is still the
sample. Weather and news are whatever the document holds; nothing here fetches anything.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pcwall import engine, model

# The settings sections a screen needs to draw itself. settings.parent_pin and
# settings.parent_pin_is_sample are deliberately not among them; see status() below.
PUBLIC_SETTINGS = ("screensaver", "night", "display", "stars", "calendar", "photos", "groceries", "weather", "news")


def recent_decisions(state: dict, now, hours: int = 24) -> list:
    """Approved or declined requests from the last day, newest first, for the wall to celebrate."""
    since = now - timedelta(hours=hours)
    decided = [dict(r) for r in engine.redemptions(state)
               if r.get("status") in ("approved", "declined") and r.get("decided_at")
               and datetime.fromisoformat(r["decided_at"]) >= since]
    return sorted(decided, key=lambda r: r["decided_at"], reverse=True)


def _clock_text(stamp) -> str:
    """'7:36 AM' from an ISO timestamp; an unreadable stamp shows no time rather than failing."""
    try:
        moment = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return ""
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _whole(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def daily_history(state: dict, member_id: str, today: date, days: int = 14) -> list:
    """One member's ledger grouped by the day it was written, newest day and entry first.

    Days without activity are left out, except today, so the sheet always opens on the
    current day; at most ``days`` days are returned. Each entry carries what a kid or a
    parent needs to read the day like a receipt: time, reason, signed delta, kind, actor.
    """
    groups = {today.isoformat(): []}
    for entry in reversed(engine.ledger(state)):      # newest written first; the stable sort keeps it
        if entry.get("member") != member_id:
            continue
        day = str(entry.get("at") or "")[:10]
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        groups.setdefault(day, []).append(entry)
    yesterday = (today - timedelta(days=1)).isoformat()
    history = []
    for day in sorted(groups, reverse=True)[:days]:
        entries = sorted(groups[day], key=lambda e: str(e.get("at") or ""), reverse=True)
        if day == today.isoformat():
            label = "Today"
        elif day == yesterday:
            label = "Yesterday"
        else:
            label = date.fromisoformat(day).strftime("%a, %b %d")
        history.append({
            "date": day, "label": label, "net": sum(_whole(e.get("delta")) for e in entries),
            "entries": [{"id": e.get("id"), "time": _clock_text(e.get("at")), "reason": e.get("reason") or "",
                         "delta": _whole(e.get("delta")), "kind": e.get("kind", ""), "by": e.get("by", "")}
                        for e in entries],
        })
    return history


def reward_catalog(state: dict) -> list:
    """The active rewards with their price under "stars" (family.json's name) and "cost"
    (the name an earlier kiosk build reads)."""
    return [{**r, "stars": engine.price(r), "cost": engine.price(r)}
            for r in state.get("rewards") or [] if isinstance(r, dict) and r.get("active", True)]


def household(state: dict, family=None) -> dict:
    """Name, time zone and school rules: from the document, which the store keeps in line with
    family.json, or from the family itself for a document written before it had them."""
    stored = state.get("household") if isinstance(state.get("household"), dict) else {}
    school = stored.get("school") if isinstance(stored.get("school"), dict) else None
    if school is None and family is not None:
        rules = family.school
        school = {"kindergarten_cutoff": rules.kindergarten_cutoff, "year_start_month": rules.year_start_month}
    return {"name": stored.get("name") or (family.name if family is not None else "Family"),
            "timezone": stored.get("timezone") or (family.timezone if family is not None else None),
            "school": school or {}}


def status(state: dict) -> dict:
    """What a screen may know about the parent PIN: that one is set, and whether it is still
    the sample that ships in family.example.json. Never the PIN or its hash."""
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    stored = settings.get("parent_pin")
    return {"pin_set": isinstance(stored, str) and bool(stored),
            "pin_is_sample": bool(settings.get("parent_pin_is_sample"))}


def public_settings(state: dict) -> dict:
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    return {section: deepcopy(settings[section]) for section in PUBLIC_SETTINGS
            if isinstance(settings.get(section), dict)}


def _now_for(state: dict, family) -> datetime:
    zone = household(state, family).get("timezone")
    try:
        return datetime.now(ZoneInfo(zone)) if zone else datetime.now()
    except (KeyError, ValueError, OSError):
        return datetime.now()


def state_document(store, family=None, now=None) -> dict:
    """The JSON state for /api/state: the store's current document, worked out for a screen.

    ``store`` is the Store (its ``current()``, ``family`` and ``now()`` are used) or, for a
    caller that already holds one, the state dict itself; ``family`` and ``now`` override
    what the store knows. Nothing the screen gets back can be used to pass as a parent.
    """
    state = store.current() if hasattr(store, "current") else store
    if family is None:
        family = getattr(store, "family", None)
    if now is None:
        now = store.now() if hasattr(store, "now") else _now_for(state, family)
    today = now.date()
    home = household(state, family)
    kids = []
    for kid in engine.kids(state):
        board = engine.kid_board(state, kid["id"], today)
        board["daily_history"] = daily_history(state, kid["id"], today)
        kids.append(board)
    calendar = state.get("calendar") if isinstance(state.get("calendar"), dict) else {}
    photos = state.get("photos") if isinstance(state.get("photos"), dict) else {}
    settings = public_settings(state)
    return {
        "schema": state.get("schema"),
        "revision": state.get("revision", 0), "now": now.isoformat(), "today": today.isoformat(),
        "household": home,
        "members": [model.profile(m, today, home["school"]) for m in state["members"]],
        "kids": kids,
        "meals": engine.week_meals(state, today),
        "meal_ideas": engine.meal_ideas(state, today),
        "groceries": engine.grocery_list(state),
        "notes": engine.note(state),
        "rewards": reward_catalog(state),
        "pending": [dict(r) for r in engine.redemptions(state) if r.get("status") == "pending"],
        "decided": recent_decisions(state, now),
        "calendar": {"events": list(calendar.get("events") or []), "synced_at": calendar.get("synced_at")},
        "weather": deepcopy(state.get("weather")) if isinstance(state.get("weather"), dict) else {},
        "news": deepcopy(state.get("news")) if isinstance(state.get("news"), dict) else {},
        "photos": [{"url": f"/photos/{p['file']}", "width": p.get("width"), "height": p.get("height")}
                   for p in photos.get("items") or [] if isinstance(p, dict) and p.get("file")],
        # The kiosk reads these two at the top level; a phone's settings sheet reads them all.
        "screensaver": settings.get("screensaver", {}),
        "night": settings.get("night", {}),
        "settings": settings,
        "status": status(state),
    }

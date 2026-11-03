"""Focused unit tests for pcwall.engine, pcwall.ops and pcwall.views (Wave 2.3).

Every test runs the real ops against a Store seeded from config/family.example.json under
tmp_path, so the document has the shape config.default_document gives it (stars.ledger,
reward_requests, a dict note, rewards priced in "stars"). Covers: ticking and unticking a
configured kid's step, the award limit and ledger entries, requesting, approving and
declining a reward, meals, groceries and the note, settings_set refusing the parent PIN,
set_pin checking the current PIN, a state document that never carries the PIN, the ledger
living in either place, an empty top-level ledger deferring to a populated stars.ledger,
groceries.keep_checked_hours refused when it isn't whole hours and tolerated by maintain()
when it is bad anyway, and the absence of every private hook. No network, no wall clock.
"""
from __future__ import annotations

import inspect
import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

# The repo root goes on sys.path first, so `python -m pytest tests/test_engine.py` finds the
# pcwall package from any working directory and without PYTHONPATH or an installed package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pcwall import access, config, engine, ops, views  # noqa: E402
from pcwall.engine import WallError  # noqa: E402
from pcwall.store import Store  # noqa: E402

EXAMPLE = ROOT / "config" / "family.example.json"
NOW = datetime(2026, 11, 2, 7, 30, 0)        # a Monday morning: every sample routine is on today
PARENT = access.Principal("parent", "alex")
KIOSK = access.Principal("kiosk", device="dv_kitchen", device_name="Kitchen")
MORNING = ["dressed", "breakfast", "teeth", "backpack"]


class Limiter:
    def __init__(self):
        self.failures = 0

    def blocked(self):
        return False

    def fail(self):
        self.failures += 1


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("PCWALL_CONFIG_DIR", raising=False)
    return Store(tmp_path / "data" / "pcwall.json", config.load_family(EXAMPLE), now=NOW, clock=lambda: NOW)


def run(store, name, args=None, caller=PARENT, ctx=None):
    spec = ops.OPS[name]
    ctx = ctx or SimpleNamespace(pin_limiter=Limiter(), pairing=None, bus=None)
    return store.transact(lambda state, now: spec.fn(state, args or {}, caller, now, ctx), kinds=spec.kinds)


def seed_stars(store, member, amount):
    store.transact(lambda state, now: engine.post(state, member, amount, "award", "seed", actor="alex", now=now))


# --- complete / uncomplete -------------------------------------------------------------


def test_complete_and_uncomplete_toggle_todays_step(store):
    ticked = run(store, "complete", {"member": "sam", "item": "step:sam.morning:teeth"}, caller=KIOSK)
    state = store.current()
    assert (ticked["stars"], ticked["routine_done"], engine.balance(state, "sam")) == (1, False, 1)
    assert state["stars"]["ledger"][0]["by"] == "wall" and state["stars"]["balances"]["sam"] == 1
    board = {r["id"]: r for r in engine.kid_board(state, "sam", NOW.date())["routines"]}
    assert (board["sam.morning"]["done"], board["sam.morning"]["kind"]) == (1, "morning")

    unticked = run(store, "uncomplete", {"member": "sam", "item": "step:sam.morning:teeth"}, caller=KIOSK)
    state = store.current()
    assert (unticked["stars"], engine.balance(state, "sam"), state["completions"][NOW.date().isoformat()]["sam"]) == (-1, 0, {})
    with pytest.raises(WallError):
        run(store, "complete", {"member": "sam", "item": "step:sam.morning:teeth", "day": "yesterday"}, caller=KIOSK)
    with pytest.raises(WallError):
        run(store, "complete", {"member": "alex", "item": "step:sam.morning:teeth"})


def test_finishing_the_routine_and_the_day_pays_once(store):
    results = [run(store, "complete", {"member": "riley", "item": f"step:riley.morning:{step}"}) for step in MORNING]
    assert [r["routine_done"] for r in results] == [False, False, False, True] and results[-1]["streak"] == 1
    for routine, steps in (("after_school", ["unpack", "homework", "tidy"]), ("bedtime", ["bath", "pajamas", "teeth", "story"])):
        for step in steps:
            last = run(store, "complete", {"member": "riley", "item": f"step:riley.{routine}:{step}"})
    state = store.current()
    assert (last["daily_bonus"], engine.balance(state, "riley")) == (engine.DAILY_BONUS, 4 + 4 + 4 + engine.DAILY_BONUS)
    assert run(store, "complete", {"member": "riley", "item": "step:riley.bedtime:story"})["already"] is True


# --- stars and rewards ------------------------------------------------------------------


def test_award_stars_respects_max_award_and_writes_the_ledger(store):
    with pytest.raises(WallError, match="5 stars"):
        run(store, "award_stars", {"who": "riley", "amount": 6})
    awarded = run(store, "award_stars", {"who": "the twins", "amount": 3, "reason": "Helped with dinner"})
    state = store.current()
    assert awarded["balances"] == {"riley": 3, "taylor": 3}
    assert [(e["member"], e["delta"], e["kind"], e["by"]) for e in state["stars"]["ledger"]] == [
        ("riley", 3, "award", "alex"), ("taylor", 3, "award", "alex")]
    assert state["stars"]["balances"] == {"sam": 0, "riley": 3, "taylor": 3} and "ledger" not in state
    assert "kiosk" not in ops.OPS["award_stars"].roles


def test_request_approve_and_decline_reward(store):
    seed_stars(store, "sam", 60)
    request = run(store, "request_reward", {"member": "sam", "reward": "dessert_pick"}, caller=KIOSK)
    state = store.current()
    assert (request["status"], request["cost"], engine.available(state, "sam")) == ("pending", 20, 40)
    assert state["reward_requests"][0]["id"] == request["id"] and "redemptions" not in state
    with pytest.raises(WallError, match="10 more stars"):
        run(store, "request_reward", {"member": "sam", "reward": "Pick the movie"}, caller=KIOSK)

    with pytest.raises(WallError, match="PIN"):
        run(store, "approve_reward", {"id": request["id"]}, caller=KIOSK)
    approved = run(store, "approve_reward", {"id": request["id"], "pin": "1234"}, caller=KIOSK)
    state = store.current()
    assert (approved["status"], approved["decided_by"], engine.balance(state, "sam")) == ("approved", "parent at the wall", 40)
    assert state["stars"]["ledger"][-1]["ref"] == f"redeem:{request['id']}"

    second = run(store, "request_reward", {"member": "sam", "reward": "stay_up_late"})
    declined = run(store, "decline_reward", {"id": second["id"]})
    state = store.current()
    assert (declined["status"], engine.balance(state, "sam"), engine.available(state, "sam")) == ("declined", 40, 40)
    assert {d["id"] for d in views.recent_decisions(state, NOW)} == {second["id"], request["id"]}


# --- meals, groceries and the note ------------------------------------------------------


def test_meals_groceries_and_note(store):
    run(store, "set_meal", {"day": "tomorrow", "title": "Tacos", "notes": "soft shells"})
    week = views.state_document(store)["meals"]
    assert (week[1]["label"], week[1]["meal"]["title"], week[0]["meal"]) == ("Tomorrow", "Tacos", None)
    assert run(store, "clear_meal", {"day": "tomorrow"})["title"] == "Tacos"
    with pytest.raises(WallError):
        run(store, "clear_meal", {"day": "tomorrow"})

    added = run(store, "grocery_add", {"items": "milk, 2 bananas"}, caller=KIOSK)
    assert added["added"] == ["milk", "bananas"] and ops.OPS["add_groceries"] is ops.OPS["grocery_add"]
    run(store, "grocery_check", {"items": ["milk"]}, caller=KIOSK)
    items = engine.grocery_list(store.current())
    assert [(g["name"], g["qty"], g["aisle"], g["checked"]) for g in items] == [
        ("bananas", "2", "produce", False), ("milk", None, "dairy", True)]
    assert run(store, "grocery_remove", {"items": ["banana"]}) == {"removed": ["bananas"]}
    assert run(store, "grocery_clear_checked") == {"cleared": 1} and store.current()["groceries"] == []

    note = run(store, "note_set", {"text": "Dentist  on Friday"})
    assert (note["text"], note["by"], views.state_document(store)["notes"]["text"]) == ("Dentist on Friday", "alex", "Dentist on Friday")
    assert run(store, "note_set", {"text": ""})["text"] == ""


# --- settings and the parent PIN --------------------------------------------------------


def test_settings_set_rejects_parent_pin(store):
    before = store.current()["settings"]["parent_pin"]
    for attempt in ({"parent_pin": "9999"}, {"settings": {"parent_pin": "9999"}},
                    {"stars": {"parent_pin": "9999"}}, {"parent_pin_is_sample": False}):
        with pytest.raises(WallError, match="set_pin"):
            run(store, "settings_set", attempt)
    assert store.current()["settings"]["parent_pin"] == before
    changed = run(store, "settings_set", {"night": {"start": "21:30"}})
    assert changed == {"settings": {"night": {"start": "21:30", "end": "06:00"}}}
    for bad in ({"night": {"start": "25:00"}}, {"telemetry": {"on": True}}, {"stars": {"max_award": 0}}):
        with pytest.raises(WallError):
            run(store, "settings_set", bad)


def test_set_pin_checks_the_current_pin(store):
    with pytest.raises(WallError):
        run(store, "set_pin", {"pin": "5678"})
    with pytest.raises(WallError):
        run(store, "set_pin", {"pin": "5678\n", "current_pin": "1234"})
    result = run(store, "set_pin", {"pin": "5678", "current_pin": "1234"})
    settings = store.current()["settings"]
    assert result == {"pin_set": True, "pin_is_sample": False} and settings["parent_pin_is_sample"] is False
    assert config.verify_pin("5678", settings["parent_pin"]) and not config.verify_pin("1234", settings["parent_pin"])


def test_state_document_never_carries_the_pin(store):
    live = deepcopy(store.current())
    document = views.state_document(store)
    dump = json.dumps(document)
    assert store.current()["settings"]["parent_pin"] not in dump
    assert "parent_pin" not in dump and "pbkdf2" not in dump
    assert document["status"] == {"pin_set": True, "pin_is_sample": True}
    assert "parent_pin" not in document["settings"] and document["night"] == {"start": "21:00", "end": "06:00"}
    profiles = {m["id"]: m for m in document["members"]}
    assert (profiles["riley"]["grade"], profiles["riley"]["twin"], "grade" in profiles["alex"]) == ("1st grade", "taylor", False)
    assert [k["member"] for k in document["kids"]] == ["sam", "riley", "taylor"]
    assert [(r["id"], r["stars"], r["cost"]) for r in document["rewards"]][0] == ("movie_pick", 50, 50)
    assert document["household"]["name"] == "The Sample Family" and store.current() == live
    assert views.state_document(live, store.family, NOW)["today"] == "2026-11-02"


# --- the ledger lives in either place ---------------------------------------------------


def test_ledger_in_stars_or_at_top_level():
    members = [{"id": "kid", "name": "Kid", "role": "kid", "color": "blue"}]
    legacy = {"members": members, "ledger": [{"id": "tx_1", "at": NOW.isoformat(), "member": "kid", "delta": 3,
                                                "kind": "award", "reason": "old", "by": "alex"}], "redemptions": []}
    engine.post(legacy, "kid", 2, "award", "new", actor="alex", now=NOW)
    assert (engine.balance(legacy, "kid"), len(legacy["ledger"]), "stars" in legacy) == (5, 2, False)

    seeded = {"members": members, "stars": {"balances": {"kid": 0}, "ledger": []}}
    engine.post(seeded, "kid", 2, "award", "new", actor="alex", now=NOW)
    assert (engine.balance(seeded, "kid"), seeded["stars"]["balances"]["kid"], "ledger" in seeded) == (2, 2, False)
    assert engine.history(seeded, "kid") == seeded["stars"]["ledger"]

    bare = {"members": members}
    assert engine.balance(bare, "kid") == 0 and engine.ledger(bare) == [] and "stars" not in bare
    engine.post(bare, "kid", 1, "award", "first", actor="alex", now=NOW)
    assert bare["stars"]["ledger"][0]["delta"] == 1

    old = {"id": "tx_0", "at": "2025-01-01T08:00:00", "member": "kid", "delta": 4, "kind": "award", "reason": "x", "by": "alex"}
    seeded["stars"]["ledger"].insert(0, old)
    archived = engine.compact_ledger(seeded, NOW)
    assert archived == [old] and [e["kind"] for e in seeded["stars"]["ledger"]] == ["carried", "award"]
    assert engine.balance(seeded, "kid") == 6


def test_empty_top_level_ledger_defers_to_populated_stars_ledger():
    """Regression: an empty state["ledger"] beside a populated stars.ledger used to win, so
    every balance read 0 and new stars were posted to the empty list, splitting the accounting.
    The populated list is the active one for reads, posts and the roll-up alike."""
    members = [{"id": "kid", "name": "Kid", "role": "kid", "color": "blue"}]
    earned = {"id": "tx_1", "at": NOW.isoformat(), "member": "kid", "delta": 3, "kind": "award", "reason": "old", "by": "alex"}
    split = {"members": members, "ledger": [], "stars": {"balances": {"kid": 3}, "ledger": [earned]}}
    assert engine.ledger(split) is split["stars"]["ledger"]
    assert (engine.balance(split, "kid"), engine.history(split, "kid")) == (3, [earned])

    engine.post(split, "kid", 2, "award", "new", actor="alex", now=NOW)
    assert (split["ledger"], len(split["stars"]["ledger"]), engine.balance(split, "kid"),
            split["stars"]["balances"]["kid"]) == ([], 2, 5, 5)

    old = {"id": "tx_0", "at": "2025-01-01T08:00:00", "member": "kid", "delta": 4, "kind": "award", "reason": "x", "by": "alex"}
    split["stars"]["ledger"].insert(0, old)
    assert engine.compact_ledger(split, NOW) == [old]
    assert "ledger" not in split and [e["kind"] for e in split["stars"]["ledger"]] == ["carried", "award", "award"]
    assert engine.balance(split, "kid") == 9

    both_empty = {"members": members, "ledger": [], "stars": {"ledger": []}}
    engine.post(both_empty, "kid", 1, "award", "first", actor="alex", now=NOW)
    assert (both_empty["ledger"], [e["delta"] for e in both_empty["stars"]["ledger"]]) == ([], [1])

    legacy_empty = {"members": members, "ledger": []}
    engine.post(legacy_empty, "kid", 1, "award", "first", actor="alex", now=NOW)
    assert len(legacy_empty["ledger"]) == 1 and "stars" not in legacy_empty

    both_full = {"members": members, "ledger": [dict(earned)], "stars": {"ledger": [dict(old)]}}
    assert engine.ledger(both_full) is both_full["ledger"] and engine.balance(both_full, "kid") == 3


@pytest.mark.parametrize("top_active", [True, False])
def test_zero_sum_compaction_purges_secondary_ledger(top_active):
    old = NOW - timedelta(days=engine.LEDGER_DETAIL_DAYS + 1)
    entries = [{"id": "tx_earned", "at": old.isoformat(), "member": "kid", "delta": 4, "kind": "award"},
               {"id": "tx_undone", "at": old.isoformat(), "member": "kid", "delta": -4, "kind": "undo"}]
    stale = {"id": "tx_stale", "at": NOW.isoformat(), "member": "kid", "delta": 99, "kind": "award"}
    state = {"ledger": entries if top_active else [],
             "stars": {"ledger": [stale] if top_active else entries, "balances": {"kid": 0}}}
    assert engine.ledger(state) is entries
    assert engine.balance(state, "kid") == 0
    assert engine.compact_ledger(state, NOW) == entries
    assert engine.balance(state, "kid") == 0
    assert engine.ledger(state) == []
    if top_active:
        assert state["ledger"] == []
        assert "ledger" not in state["stars"]
    else:
        assert state["stars"]["ledger"] == []
        assert "ledger" not in state
    assert state["stars"]["balances"] == {"kid": 0}
    assert engine.compact_ledger(state, NOW) == []

    posted = engine.post(state, "kid", 2, "award", "new", actor="alex", now=NOW)
    assert engine.ledger(state) == [posted]
    assert engine.balance(state, "kid") == 2
    assert state["stars"]["balances"] == {"kid": 2}


# --- groceries.keep_checked_hours: refused when bad, tolerated when bad anyway -----------


def test_settings_set_validates_keep_checked_hours(store):
    """Regression: settings_set stored null or text for groceries.keep_checked_hours and the
    next maintain() crashed building a timedelta from it. Whole hours from 0 to the cap only."""
    before = deepcopy(store.current()["settings"].get("groceries"))
    for bad in (None, "24", 24.5, True, -1, engine.CHECKED_KEPT_HOURS_MAX + 1, [24], {"hours": 24}):
        with pytest.raises(WallError, match="groceries.keep_checked_hours"):
            run(store, "settings_set", {"groceries": {"keep_checked_hours": bad}})
    assert store.current()["settings"].get("groceries") == before

    changed = run(store, "settings_set", {"groceries": {"keep_checked_hours": 0}})
    assert changed["settings"]["groceries"]["keep_checked_hours"] == 0
    run(store, "settings_set", {"groceries": {"keep_checked_hours": engine.CHECKED_KEPT_HOURS_MAX}})
    assert store.current()["settings"]["groceries"]["keep_checked_hours"] == engine.CHECKED_KEPT_HOURS_MAX
    assert engine.keep_checked_hours(store.current()) == engine.CHECKED_KEPT_HOURS_MAX
    assert ops.SETTING_RULES[("groceries", "keep_checked_hours")](36) is True


def test_maintain_tolerates_a_bad_keep_checked_hours_setting():
    """Regression: maintain() built the timedelta straight from the stored setting, so a null
    or text value stopped the daily tidy-up. Invalid stored values fall back to
    CHECKED_KEPT_HOURS; any nonnegative whole number of hours is honoured."""
    def document(setting):
        def checked(hours_ago, name):
            return {"id": f"g_{name}", "name": name, "checked": True,
                    "checked_at": (NOW - timedelta(hours=hours_ago)).isoformat()}
        return {"settings": {"groceries": {"keep_checked_hours": setting}},
                "groceries": [checked(12, "milk"), checked(30, "eggs"), {"id": "g_bread", "name": "bread", "checked": False}]}

    for bad in (None, "24", 24.5, True, -1, [24], {"hours": 24}):
        state = document(bad)
        assert engine.keep_checked_hours(state) == engine.CHECKED_KEPT_HOURS, bad
        assert engine.maintain(state, NOW) == {"groceries": 1}, bad       # eggs (30h) go, milk (12h) stays
        assert [g["name"] for g in state["groceries"]] == ["milk", "bread"], bad

    state = document(6)
    assert engine.keep_checked_hours(state) == 6 and engine.maintain(state, NOW) == {"groceries": 2}
    assert [g["name"] for g in state["groceries"]] == ["bread"]
    assert engine.maintain(document(0), NOW) == {"groceries": 2}
    assert engine.maintain(document(engine.CHECKED_KEPT_HOURS_MAX), NOW) == {}

    unset = {"groceries": document(None)["groceries"]}
    assert engine.keep_checked_hours(unset) == engine.CHECKED_KEPT_HOURS and engine.maintain(unset, NOW) == {"groceries": 1}


@pytest.mark.parametrize("hours", [721, 1440])
def test_maintain_honours_stored_hours_above_settings_api_cap(hours):
    state = {"settings": {"groceries": {"keep_checked_hours": hours}},
             "groceries": [{"name": name, "checked": True,
                            "checked_at": (NOW - timedelta(hours=age)).isoformat()}
                           for name, age in (("recent", hours - 1), ("boundary", hours), ("expired", hours + 1))]}
    assert engine.keep_checked_hours(state) == hours
    assert engine.maintain(state, NOW) == {"groceries": 1}
    assert [g["name"] for g in state["groceries"]] == ["recent", "boundary"]
    assert state["settings"]["groceries"]["keep_checked_hours"] == hours


# --- no private hooks ---------------------------------------------------------------------


def test_no_telegram_home_control_or_math_hooks_remain():
    source = "\n".join(inspect.getsource(module) for module in (engine, ops, views)).lower()
    for word in ("telegram", "home_control", "twilio", "automated_step", "outbox", "intercom",
                 "video_message", "emergency", "math", "weather_client", "trips_tool", "import requests"):
        assert word not in source, word
    assert not {"outbox", "ack", "home_status", "home_light_set", "home_run_scene", "send_intercom",
                "trigger_emergency", "watch_video_message", "restart"} & set(ops.OPS)
    assert {"complete", "uncomplete", "award_stars", "request_reward", "approve_reward", "decline_reward",
            "set_meal", "clear_meal", "grocery_add", "grocery_check", "grocery_remove", "grocery_clear_checked",
            "note_set", "settings_set", "pairing_code", "revoke_device", "rename_device", "set_pin"} <= set(ops.OPS)
    assert not any(hasattr(engine, name) for name in ("notify", "outbox", "ack", "OUTBOX_KEPT_DAYS"))

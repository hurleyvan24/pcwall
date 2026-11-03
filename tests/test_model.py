"""Focused unit tests for pcwall.model and pcwall.store (Wave 2.2).

Covers: school grades under configurable rules (cutoff, leap-day birthdays, the
school-year boundary), profiles that take SchoolRules, per-kid routine copies with
overrides, the purge of the hardcoded family constants, and a Store that seeds a
missing document from the family config, loads the config itself when not given one,
keeps an existing document and brings a legacy one in line with the config. No
network, no wall clock, no writes outside tmp_path.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcwall import config, model
from pcwall.config import SchoolRules, load_family, verify_pin
from pcwall.store import Store

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config" / "family.example.json"
TODAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 7, 30, 0)
SEPTEMBER_BORN = "2019-09-02"   # the sample twins: five the day after a September 1 cutoff


@pytest.fixture
def family():
    return load_family(EXAMPLE)


@pytest.fixture
def wall_path(tmp_path, monkeypatch) -> Path:
    """data/pcwall.json under tmp_path, with no config-directory override in the way."""
    monkeypatch.delenv("PCWALL_CONFIG_DIR", raising=False)
    return tmp_path / "data" / "pcwall.json"


# --- Task 2.2.1: configurable school rules --------------------------------------------


def test_grade_by_configurable_cutoff():
    assert model.school_grade(SEPTEMBER_BORN, TODAY, SchoolRules("09-01", 8)) == 1
    assert model.school_grade(SEPTEMBER_BORN, TODAY, SchoolRules("12-31", 8)) == 2
    pair = SimpleNamespace(kindergarten_cutoff=(9, 1), year_start_month=8)
    assert model.school_grade(SEPTEMBER_BORN, TODAY, pair) == 1
    assert model.school_grade(SEPTEMBER_BORN, TODAY) == 1   # no rules: the SchoolRules defaults


def test_leap_day_birthday():
    born = "2016-02-29"
    assert model.school_grade(born, TODAY, SchoolRules("03-01", 8)) == 5   # five by March 1, 2021
    assert model.school_grade(born, TODAY, SchoolRules("02-28", 8)) == 4   # a day too young in 2021
    assert model.days_until_birthday(born, date(2026, 2, 28)) == 1        # kept on March 1 off leap years


def test_year_start_month_boundary():
    august = SchoolRules("09-01", 8)
    assert model.school_grade(SEPTEMBER_BORN, date(2026, 7, 31), august) == 0
    assert model.school_grade(SEPTEMBER_BORN, date(2026, 8, 1), august) == 1
    assert model.school_grade(SEPTEMBER_BORN, date(2026, 8, 31), SchoolRules("09-01", 9)) == 0


def test_profile_takes_school_rules(family):
    riley = family.member("riley")
    view = model.profile(riley, TODAY, family.school)
    assert (view["age"], view["grade"], view["twin"], view["colors"]) == (7, "1st grade", "taylor", model.PALETTE["blue"])
    assert model.profile(riley, TODAY, SchoolRules("12-31", 8))["grade"] == "2nd grade"
    assert "grade" not in model.profile(family.member("alex"), TODAY, family.school)


# --- Task 2.2.2: routines from config and constant purge ------------------------------


def test_routines_for_returns_fresh_copy_per_kid(family):
    riley = model.routines_for("riley", family)
    taylor = model.routines_for("taylor", family)
    assert [r["id"] for r in riley] == ["morning", "after_school", "bedtime"]
    assert riley == taylor and riley[0]["steps"] is not taylor[0]["steps"]
    riley[0]["steps"].append({"id": "extra", "title": "Extra", "emoji": "", "stars": 1})
    assert len(family.routines[0]["steps"]) == 4
    assert len(model.routines_for("riley", family)[0]["steps"]) == 4
    assert model.routines_for("alex", family) == []
    with pytest.raises(KeyError):
        model.routines_for("nobody", family)


def test_routines_for_applies_bedtime_override(family):
    sam = {r["id"]: r for r in model.routines_for("sam", family)}
    assert (sam["bedtime"]["start"], sam["bedtime"]["end"]) == ("19:30", "21:00")
    assert [s["id"] for s in sam["bedtime"]["steps"]] == ["bath", "pajamas", "teeth", "story"]
    riley = {r["id"]: r for r in model.routines_for("riley", family)}
    assert (riley["bedtime"]["start"], riley["bedtime"]["end"]) == ("19:00", "20:30")


def test_no_member_constants_in_module():
    removed = {"MEMBERS", "ROUTINES", "REWARDS", "LATER_BEDTIME", "DEFAULT_PARENT_PIN",
               "KINDERGARTEN_CUTOFF", "SCHOOL_YEAR_START_MONTH", "DEFAULT_SETTINGS"}
    assert removed.isdisjoint(dir(model))
    assert set(model.PALETTE) == set(config.PALETTE)
    assert model.default_rewards() == [] and model.AUTOMATED_STEP_IDS == () and model.RETIRED_REWARDS == ()


# --- Store: seeding and loading -------------------------------------------------------


def test_store_seeds_from_family_when_missing(family, wall_path):
    store = Store(wall_path, family, now=NOW)
    state = store.current()
    assert wall_path.is_file() and store.zone.key == "America/New_York"
    assert (state["revision"], state["created_at"]) == (0, NOW.isoformat())
    assert [m["id"] for m in state["members"]] == ["sam", "riley", "taylor", "alex", "jordan", "family"]
    routines = {r["id"]: r for r in state["routines"]}
    assert (routines["sam.bedtime"]["start"], routines["riley.bedtime"]["start"]) == ("19:30", "19:00")
    assert state["weather"]["location"] is None
    assert state["settings"]["display"] == {"default_preset": "auto", "kid_columns": "auto"}
    assert verify_pin("1234", state["settings"]["parent_pin"])


def test_store_loads_family_from_config_dir_when_not_given(wall_path):
    store = Store(wall_path, now=NOW)
    assert (wall_path.parent.parent / "config" / "family.json").is_file()
    assert store.family.name == "The Sample Family"
    assert store.current()["household"]["timezone"] == "America/New_York"


def test_store_keeps_existing_document_and_fills_new_keys(family, wall_path):
    Store(wall_path, family, now=NOW)
    document = json.loads(wall_path.read_text(encoding="utf-8"))
    document["revision"] = 7
    document["weather"]["location"] = {"city": "Boston", "lat": 42.36, "lon": -71.06}
    document["settings"]["display"]["default_preset"] = "hd_720p"
    document["settings"]["parent_pin"] = "pbkdf2_sha256$1$00$00"
    del document["settings"]["news"]
    wall_path.write_text(json.dumps(document), encoding="utf-8")

    state = Store(wall_path, family, now=datetime(2026, 12, 25, 8, 0, 0)).current()
    assert (state["revision"], state["created_at"]) == (7, NOW.isoformat())
    assert state["weather"]["location"]["city"] == "Boston"
    assert state["settings"]["display"]["default_preset"] == "hd_720p"
    assert state["settings"]["parent_pin"] == "pbkdf2_sha256$1$00$00"
    assert state["settings"]["news"]["feeds"] == [dict(feed) for feed in config.DEFAULT_NEWS_FEEDS]


def test_store_syncs_legacy_document_with_config(family, wall_path):
    legacy = {
        "schema_version": 1, "revision": 3,
        "created_at": "2025-01-01T00:00:00", "updated_at": "2025-01-01T00:00:00",
        "members": [
            {"id": "riley", "name": "Riley", "role": "kid", "birthdate": "2015-06-15",
             "color": "violet", "emoji": "", "aliases": []},
            {"id": "casey", "name": "Casey", "role": "kid", "birthdate": "2018-09-20",
             "color": "green", "emoji": "", "aliases": []},
        ],
        "routines": [
            {"id": "riley.bedtime", "member": "riley", "kind": "bedtime", "title": "Bed", "emoji": "",
             "days": ["mon"], "start": "19:30", "end": "21:00", "bonus": 5, "active": False, "steps": []},
        ],
        "rewards": [{"id": "cash_5", "title": "$5 Cash", "emoji": "", "cost": 75, "active": True}],
        "settings": {"parent_pin": "pbkdf2_sha256$1$00$00", "stars": {"streak_milestones": {"3": 5}}},
    }
    wall_path.parent.mkdir(parents=True)
    wall_path.write_text(json.dumps(legacy), encoding="utf-8")

    state = Store(wall_path, family, now=NOW).current()
    assert (state["schema"], state["revision"], state["household"]["name"]) == (1, 3, "The Sample Family")
    members = {m["id"]: m for m in state["members"]}
    assert (members["riley"]["color"], members["riley"]["birthdate"]) == ("blue", "2019-09-02")
    assert "casey" in members and "sam" in members
    routines = {r["id"]: r for r in state["routines"]}
    bedtime = routines["riley.bedtime"]
    assert (bedtime["start"], bedtime["active"], bedtime["bonus"], len(bedtime["steps"])) == ("19:00", False, 0, 4)
    assert routines["sam.bedtime"]["start"] == "19:30"
    assert [r["id"] for r in state["rewards"]] == ["cash_5", "movie_pick", "dessert_pick", "stay_up_late"]
    assert state["settings"]["stars"]["streak_milestones"] == {}
    assert state["stars"]["balances"] == {"sam": 0, "riley": 0, "taylor": 0}

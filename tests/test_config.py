"""Focused unit tests for pcwall.config (Wave 2.1).

Covers: the sample family loads, palette and id rules, default_document settings,
ensure_family_file, validation errors naming the exact JSON path, per-kid overrides,
PIN hashing and the step star default. No network, no wall clock, no writes outside
tmp_path.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from pcwall import config
from pcwall.config import (
    ID_PATTERN,
    PALETTE,
    ConfigError,
    Family,
    SchoolRules,
    default_document,
    ensure_family_file,
    family_to_members,
    hash_pin,
    load_family,
    verify_pin,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config" / "family.example.json"
NOW = datetime(2026, 11, 2, 7, 30, 0)
SAMPLE_PIN = "1234"


@pytest.fixture
def sample() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def family() -> Family:
    return load_family(EXAMPLE)


def _write(tmp_path: Path, data: dict) -> Path:
    target = tmp_path / "family.json"
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return target


def _error_path(tmp_path: Path, data: dict) -> str:
    """Load a mutated document and return the JSON path the ConfigError names."""
    with pytest.raises(ConfigError) as info:
        load_family(_write(tmp_path, data))
    assert info.value.path in str(info.value)
    return info.value.path


# --- Task 2.1.1: happy path and sample file -------------------------------------------


def test_sample_family_loads(family):
    assert isinstance(family, Family)
    assert family.schema == 1
    assert family.name == "The Sample Family"
    assert family.timezone == "America/New_York"
    assert family.school == SchoolRules(kindergarten_cutoff="09-01", year_start_month=8)
    assert [m["id"] for m in family.members] == ["sam", "riley", "taylor", "alex", "jordan", "family"]
    assert [m["id"] for m in family.kids] == ["sam", "riley", "taylor"]
    assert [m["id"] for m in family.parents] == ["alex", "jordan"]
    assert [r["id"] for r in family.routines] == ["morning", "after_school", "bedtime"]
    assert [r["id"] for r in family.rewards] == ["movie_pick", "dessert_pick", "stay_up_late"]
    assert family.parent_pin == SAMPLE_PIN


def test_members_have_palette_colors(family):
    assert PALETTE == frozenset({"violet", "blue", "green", "orange", "slate", "stone"})
    members = family_to_members(family)
    assert len(members) == 6
    for member in members:
        assert member["color"] in PALETTE
        assert ID_PATTERN.match(member["id"])
    assert len({m["color"] for m in members}) == 6  # the sample uses every palette key once
    members[0]["name"] = "Changed"  # copies, never the loaded objects
    assert family.members[0]["name"] == "Sam"


def test_default_document_contains_display_weather_news_settings(family):
    doc = default_document(family, NOW)
    settings = doc["settings"]
    assert settings["display"] == {"default_preset": "auto", "kid_columns": "auto"}
    assert settings["weather"] == {"enabled": True, "units": "fahrenheit", "refresh_minutes": 30}
    assert settings["news"] == {
        "enabled": True,
        "feeds": [{"title": "NPR", "url": "https://feeds.npr.org/1001/rss.xml"}],
        "refresh_minutes": 15,
        "max_items": 30,
        "ticker_speed": 60,
    }
    for key in ("screensaver", "night", "stars", "calendar", "photos", "groceries", "parent_pin"):
        assert key in settings, key
    for key in ("members", "routines", "rewards", "groceries", "calendar", "notes", "photos", "weather", "news"):
        assert key in doc, key
    assert doc["revision"] == 0
    assert doc["created_at"] == NOW.isoformat()
    assert doc["household"]["timezone"] == "America/New_York"
    assert doc["members"] == family_to_members(family)
    assert {r["member"] for r in doc["routines"]} == {"sam", "riley", "taylor"}
    assert len(doc["routines"]) == 9  # three kids times three routines
    assert [r["id"] for r in doc["rewards"]] == ["movie_pick", "dessert_pick", "stay_up_late"]


def test_ensure_family_file_copies_example_when_missing(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "family.example.json").write_bytes(EXAMPLE.read_bytes())
    target = ensure_family_file(config_dir)
    assert target == config_dir / "family.json"
    assert target.read_bytes() == EXAMPLE.read_bytes()
    assert load_family(target).name == "The Sample Family"
    # An existing family.json is never overwritten.
    target.write_text('{"custom": true}', encoding="utf-8")
    assert ensure_family_file(str(config_dir)) == target
    assert json.loads(target.read_text(encoding="utf-8")) == {"custom": True}


def test_ensure_family_file_falls_back_to_bundled_example(tmp_path):
    config_dir = tmp_path / "not-yet-created"
    target = ensure_family_file(config_dir)
    assert target == config_dir / "family.json"
    assert load_family(target).name == "The Sample Family"


# --- Task 2.1.2: validation errors name the JSON path ----------------------------------


def test_duplicate_id(tmp_path, sample):
    sample["members"][1]["id"] = "sam"
    assert _error_path(tmp_path, sample) == "members[1].id"
    sample["members"][1]["id"] = "riley"
    sample["rewards"][2]["id"] = "movie_pick"
    assert _error_path(tmp_path, sample) == "rewards[2].id"


def test_unknown_color(tmp_path, sample):
    sample["members"][0]["color"] = "pink"
    assert _error_path(tmp_path, sample) == "members[0].color"


def test_dangling_twin(tmp_path, sample):
    sample["members"][2]["twin"] = "nobody"
    assert _error_path(tmp_path, sample) == "members[2].twin"
    sample["members"][2]["twin"] = "alex"  # exists, but is a parent
    assert _error_path(tmp_path, sample) == "members[2].twin"
    sample["members"][2]["twin"] = "taylor"  # nobody is their own twin
    assert _error_path(tmp_path, sample) == "members[2].twin"


def test_bad_time_format(tmp_path, sample):
    sample["routines"][0]["start"] = "6:30"
    assert _error_path(tmp_path, sample) == "routines[0].start"
    sample["routines"][0]["start"] = "06:30"
    sample["routines"][2]["end"] = "24:00"
    assert _error_path(tmp_path, sample) == "routines[2].end"


def test_missing_parent(tmp_path, sample):
    sample["members"] = [m for m in sample["members"] if m["role"] != "parent"]
    assert _error_path(tmp_path, sample) == "members"


def test_missing_kid(tmp_path, sample):
    sample["members"] = [m for m in sample["members"] if m["role"] != "kid"]
    sample["overrides"] = {}
    assert _error_path(tmp_path, sample) == "members"


def test_short_pin(tmp_path, sample):
    sample["parent_pin"] = "123"
    assert _error_path(tmp_path, sample) == "parent_pin"
    sample["parent_pin"] = "123456789"
    assert _error_path(tmp_path, sample) == "parent_pin"
    sample["parent_pin"] = 1234  # digits, but not a string
    assert _error_path(tmp_path, sample) == "parent_pin"


def test_bad_id_pattern(tmp_path, sample):
    sample["members"][0]["id"] = "Sam"
    assert _error_path(tmp_path, sample) == "members[0].id"
    sample["members"][0]["id"] = "sam"
    sample["routines"][1]["steps"][0]["id"] = "1st-thing"
    assert _error_path(tmp_path, sample) == "routines[1].steps[0].id"


def test_bad_day_name(tmp_path, sample):
    sample["routines"][0]["days"][2] = "wednesday"
    assert _error_path(tmp_path, sample) == "routines[0].days[2]"
    sample["routines"][0]["days"] = []
    assert _error_path(tmp_path, sample) == "routines[0].days"


def test_household_and_root_paths(tmp_path, sample):
    sample["household"]["timezone"] = "Mars/Olympus_Mons"
    assert _error_path(tmp_path, sample) == "household.timezone"
    sample["household"]["timezone"] = "America/New_York"
    sample["household"]["school"]["kindergarten_cutoff"] = "13-01"
    assert _error_path(tmp_path, sample) == "household.school.kindergarten_cutoff"
    sample["household"]["school"]["kindergarten_cutoff"] = "09-01"
    sample["schema"] = 2
    assert _error_path(tmp_path, sample) == "schema"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_family(broken)
    assert info.value.path == "$"
    with pytest.raises(ConfigError) as info:
        load_family(tmp_path / "does-not-exist.json")
    assert info.value.path == "$"


# --- Task 2.1.3: overrides, PIN hashing and the star default ---------------------------


def test_override_applies_to_one_kid_only(family):
    sam = {r["id"]: r for r in family.routines_for("sam")}
    riley = {r["id"]: r for r in family.routines_for("riley")}
    assert (sam["bedtime"]["start"], sam["bedtime"]["end"]) == ("19:30", "21:00")
    assert (riley["bedtime"]["start"], riley["bedtime"]["end"]) == ("19:00", "20:30")
    assert sam["morning"] == riley["morning"]
    assert family.routines_for("alex") == []
    with pytest.raises(KeyError):
        family.routines_for("nobody")
    # Resolved routines are copies: mutating one never leaks into the family.
    sam["morning"]["steps"].clear()
    assert len(family.routines_for("sam")[0]["steps"]) == 4
    doc = default_document(family, NOW)
    by_id = {r["id"]: r for r in doc["routines"]}
    assert by_id["sam.bedtime"]["start"] == "19:30"
    assert by_id["riley.bedtime"]["start"] == "19:00"
    assert by_id["taylor.bedtime"]["start"] == "19:00"
    assert by_id["sam.bedtime"]["member"] == "sam"
    assert by_id["sam.bedtime"]["routine"] == "bedtime"


def test_override_errors_name_their_path(tmp_path, sample):
    sample["overrides"]["nobody"] = {"bedtime": {"start": "20:00"}}
    assert _error_path(tmp_path, sample) == "overrides.nobody"
    del sample["overrides"]["nobody"]
    sample["overrides"]["sam"]["nap"] = {"start": "13:00"}
    assert _error_path(tmp_path, sample) == "overrides.sam.nap"
    del sample["overrides"]["sam"]["nap"]
    sample["overrides"]["sam"]["bedtime"]["start"] = "8pm"
    assert _error_path(tmp_path, sample) == "overrides.sam.bedtime.start"
    sample["overrides"]["sam"]["bedtime"]["start"] = "19:30"
    sample["overrides"]["sam"]["bedtime"]["colour"] = "red"
    assert _error_path(tmp_path, sample) == "overrides.sam.bedtime.colour"


def test_pin_hashed_once_then_plaintext_ignored(family):
    hashed = hash_pin(SAMPLE_PIN)
    algorithm, rounds, salt, digest = hashed.split("$")
    assert algorithm == "pbkdf2_sha256"
    assert int(rounds) == 200_000
    assert len(salt) == 32 and len(digest) == 64  # 16-byte salt and 32-byte digest, hex
    assert verify_pin(SAMPLE_PIN, hashed) is True
    assert verify_pin("4321", hashed) is False
    assert verify_pin(SAMPLE_PIN, "not-a-hash") is False
    assert hash_pin(SAMPLE_PIN, salt=bytes(16)) == hash_pin(SAMPLE_PIN, salt=bytes(16))
    assert hash_pin(SAMPLE_PIN) != hashed  # a fresh random salt each time
    doc = default_document(family, NOW)
    stored = doc["settings"]["parent_pin"]
    assert config.is_pin_hash(stored)
    assert SAMPLE_PIN not in stored
    assert verify_pin(SAMPLE_PIN, stored)
    assert doc["settings"]["parent_pin_is_sample"] is True
    # The plaintext never lands anywhere in the document except inside the hash.
    assert SAMPLE_PIN not in json.dumps({k: v for k, v in doc.items() if k != "settings"})


def test_stars_default_to_one(tmp_path, sample, family):
    morning = {s["id"]: s for s in family.routines[0]["steps"]}
    assert morning["dressed"]["stars"] == 1  # not written in the sample
    assert morning["teeth"]["stars"] == 1  # written explicitly
    after_school = {s["id"]: s for s in family.routines[1]["steps"]}
    assert after_school["homework"]["stars"] == 2
    doc = default_document(family, NOW)
    seeded = next(r for r in doc["routines"] if r["id"] == "riley.morning")
    assert {s["id"]: s["stars"] for s in seeded["steps"]}["dressed"] == 1
    sample["routines"][0]["steps"][0]["stars"] = -1
    assert _error_path(tmp_path, sample) == "routines[0].steps[0].stars"
    sample["routines"][0]["steps"][0]["stars"] = True  # bool is not a star count
    assert _error_path(tmp_path, sample) == "routines[0].steps[0].stars"


def test_check_config_prints_table_and_reports_errors(tmp_path, sample, capsys):
    assert config.main(["--check-config", "--config", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    for token in ("sam", "riley", "taylor", "alex", "jordan", "morning", "after_school",
                  "bedtime", "movie_pick", "19:30"):
        assert token in out, token
    assert SAMPLE_PIN not in out  # the check never prints the PIN
    sample["members"][1]["id"] = "sam"
    assert config.main(["--check-config", "--config", str(_write(tmp_path, sample))]) == 1
    assert "members[1].id" in capsys.readouterr().err

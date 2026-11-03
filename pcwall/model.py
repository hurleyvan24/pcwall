"""Pure family helpers: colours, dates, grades, routines and grocery aisles.

Pure data and date arithmetic: nothing here reads the disk, the network or the
clock, and nothing here knows a particular family. Members, routines, rewards, the
school rules and the parent PIN all come from config/family.json (pcwall.config);
this module only works on what it is handed. See docs/SKYLIGHT-CLONE-DESIGN.md
section 3.
"""

import copy
import re
import unicodedata
from datetime import date

from pcwall.config import SchoolRules

SCHEMA_VERSION = 1
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAYS = DAYS[:5]

# fill: rings, bars and other shapes; ink: text and badges (white on ink is at least
# 5.0:1, WCAG AA); tint: card backgrounds behind ink or dark text.
PALETTE = {
    "violet": {"fill": "#8B5CF6", "ink": "#6D28D9", "tint": "#EDE9FE"},
    "blue": {"fill": "#3B82F6", "ink": "#1D4ED8", "tint": "#DBEAFE"},
    "green": {"fill": "#22C55E", "ink": "#15803D", "tint": "#DCFCE7"},
    "orange": {"fill": "#F97316", "ink": "#C2410C", "tint": "#FFEDD5"},
    "slate": {"fill": "#64748B", "ink": "#334155", "tint": "#E2E8F0"},
    "stone": {"fill": "#78716C", "ink": "#44403C", "tint": "#E7E5E4"},
}

# The wall itself automates no step and retires no reward: both catalogs come from
# family.json. engine, ops and views still consult these names (until Wave 2.3 rewrites
# them), so they stay as empty tuples rather than as one family's step and reward ids.
AUTOMATED_STEP_IDS = ()
RETIRED_REWARDS = ()

# Aisle, then the words that put an item there. Multi-word phrases are tried first,
# then the item's last word (the head noun: "orange juice" is a drink, not produce).
AISLES = (
    ("produce", ("apple", "banana", "berry", "strawberry", "blueberry", "raspberry", "grape",
                 "orange", "lemon", "lime", "peach", "pear", "melon", "watermelon", "avocado",
                 "tomato", "potato", "onion", "garlic", "carrot", "celery", "cucumber",
                 "broccoli", "lettuce", "spinach", "kale", "pepper", "corn", "mushroom",
                 "zucchini", "cilantro", "fruit", "salad")),
    ("bakery", ("bread", "bagel", "tortilla", "bun", "muffin", "croissant", "pita", "roll")),
    ("dairy", ("milk", "cheese", "yogurt", "butter", "cream", "egg", "sour cream")),
    ("meat", ("chicken", "beef", "pork", "turkey", "bacon", "sausage", "fish", "salmon",
              "shrimp", "ham", "steak", "ground beef")),
    ("frozen", ("ice cream", "popsicle", "waffle", "frozen pizza", "frozen peas")),
    ("pantry", ("rice", "pasta", "noodle", "cereal", "flour", "sugar", "oil", "sauce", "soup",
                "bean", "broth", "oat", "oatmeal", "honey", "jam", "spice", "salt",
                "peanut butter", "olive oil")),
    ("snacks", ("chip", "cracker", "cookie", "pretzel", "popcorn", "granola", "snack")),
    ("drinks", ("juice", "water", "soda", "coffee", "tea", "sparkling water")),
    ("household", ("paper towel", "toilet paper", "tissue", "soap", "detergent", "sponge",
                   "trash bag", "foil", "battery", "shampoo", "toothpaste", "dish soap")),
)
AISLE_ORDER = tuple(name for name, _ in AISLES) + ("other",)


def normalize(text) -> str:
    """Casefolded, NFKC-normalized, single-spaced: how names and titles are compared."""
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())


def singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("oes") and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def aisle_for(name: str) -> str:
    text = normalize(name)
    for aisle, words in AISLES:
        if any(" " in word and word in text for word in words):
            return aisle
    for word in reversed(re.findall(r"[a-z]+", text)):
        for aisle, words in AISLES:
            if word in words or singular(word) in words:
                return aisle
    return "other"


def to_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def age_on(birthdate, today: date) -> int:
    born = to_date(birthdate)
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def school_rule(rules, name: str, default):
    """One field of the school rules: from a SchoolRules, any object with the same
    attributes, or the document's household.school dict. No rules means the default."""
    if rules is None:
        return default
    if isinstance(rules, dict):
        return rules.get(name, default)
    return getattr(rules, name, default)


def kindergarten_cutoff(rules) -> tuple:
    """(month, day) a child must be five by to start kindergarten that year, from the
    "MM-DD" string family.json uses or from a (month, day) pair."""
    raw = school_rule(rules, "kindergarten_cutoff", SchoolRules.kindergarten_cutoff)
    if isinstance(raw, str):
        month, day = raw.split("-")
    else:
        month, day = raw
    return int(month), int(day)


def school_grade(birthdate, today: date, rules=None) -> int:
    """0 is kindergarten, below 0 is preschool. A child who is five on or before the
    cutoff starts kindergarten that year, and the next grade applies from
    year_start_month on. ``rules`` is the household's SchoolRules; None means the
    SchoolRules defaults (September 1, August)."""
    born = to_date(birthdate)
    kindergarten = born.year + 5 + ((born.month, born.day) > kindergarten_cutoff(rules))
    start_month = int(school_rule(rules, "year_start_month", SchoolRules.year_start_month))
    school_year = today.year if today.month >= start_month else today.year - 1
    return school_year - kindergarten


def grade_label(grade: int) -> str:
    if grade < 0:
        return "Preschool"
    if grade == 0:
        return "Kindergarten"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(grade % 10 if grade > 20 else grade, "th")
    return f"{grade}{suffix} grade"


def days_until_birthday(birthdate, today: date) -> int:
    born = to_date(birthdate)

    def on(year):
        try:
            return date(year, born.month, born.day)
        except ValueError:          # a February 29 birthday in a common year
            return date(year, 3, 1)

    upcoming = on(today.year)
    if upcoming < today:
        upcoming = on(today.year + 1)
    return (upcoming - today).days


def profile(member: dict, today: date, school=None) -> dict:
    """What the wall shows for one member: colours resolved, age and grade worked out
    under the household's ``school`` rules. A member's own "grade" field wins."""
    view = {key: member.get(key) for key in ("id", "name", "role", "emoji", "twin")}
    view["color"] = member["color"]
    view["colors"] = dict(PALETTE[member["color"]])
    if member.get("birthdate"):
        view["age"] = age_on(member["birthdate"], today)
        grade = member.get("grade")
        if grade is None:
            grade = school_grade(member["birthdate"], today, school)
        view["grade"] = grade_label(grade)
        view["birthday_in"] = days_until_birthday(member["birthdate"], today)
    return view


def routines_for(member_id: str, family) -> list:
    """Every routine as one kid sees it: fresh deep copies of ``family.routines`` with
    that kid's ``family.overrides`` patched in, so the caller may change them freely.
    A member who is not a kid has no routines; an unknown id raises KeyError so a typo
    surfaces instead of rendering an empty card."""
    member = next((m for m in family.members if m["id"] == member_id), None)
    if member is None:
        raise KeyError(member_id)
    if member["role"] != "kid":
        return []
    patches = family.overrides.get(member_id) or {}
    routines = []
    for routine in family.routines:
        item = copy.deepcopy(routine)
        for key, value in (patches.get(routine["id"]) or {}).items():
            item[key] = copy.deepcopy(value)
        routines.append(item)
    return routines


def default_rewards(family=None) -> list:
    """The family's reward catalog as fresh dicts, or nothing when no family is given:
    the wall ships no catalog of its own (engine and views still ask without one)."""
    return copy.deepcopy(list(family.rewards)) if family is not None else []

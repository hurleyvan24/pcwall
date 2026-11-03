"""Display contract tests for the wall's static files (Wave 3.2).

Reads pcwall/static/app.css, index.html and app.mjs as text and checks the CSS scaling
contract: --ui-scale drives the root font-size, layout widths and card padding are in rem,
the kid grid auto-fits, the news ticker is a 2.2rem strip that scrolls on transform and
stops under reduced motion, and app.mjs wires display.mjs into boot and Settings.
No browser, no server, no network.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "pcwall" / "static"


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def mjs() -> str:
    return (STATIC / "app.mjs").read_text(encoding="utf-8")


def block_after(text: str, marker: str) -> str:
    """The balanced {...} body that follows the first occurrence of marker."""
    start = text.index(marker)
    opening = text.index("{", start)
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1:index]
    raise AssertionError(f"unbalanced block after {marker!r}")


def top_level_rule(css: str, selector: str) -> str:
    """Body of the rule whose selector starts a line, so copies inside media queries are skipped."""
    match = re.search(rf"^{re.escape(selector)}\s*\{{([^}}]*)\}}", css, re.MULTILINE)
    assert match, f"no top-level rule for {selector}"
    return match.group(1)


def declarations(body: str) -> dict[str, str]:
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    pairs = (item.split(":", 1) for item in body.split(";") if ":" in item)
    return {name.strip(): value.strip() for name, value in pairs}


# ── CSS contract ──────────────────────────────────────────────────────────────────────


def test_root_declares_ui_scale(css: str) -> None:
    root = declarations(block_after(css, ":root {"))
    assert root["--ui-scale"] == "1"
    assert next(iter(root)) == "--ui-scale", "--ui-scale leads the root variables"


def test_root_font_size_uses_ui_scale(css: str) -> None:
    root = declarations(block_after(css, ":root {"))
    assert root["font-size"] == "calc(16px * var(--ui-scale))"
    assert "font-size: 16px" not in css, "no fixed root size anywhere else"


def test_1700px_query_has_no_font_size(css: str) -> None:
    block = block_after(css, "@media (min-width: 1700px)")
    assert "font-size" not in block
    assert ":root" not in block


def test_1700px_query_widens_sidebar_in_rem(css: str) -> None:
    block = block_after(css, "@media (min-width: 1700px)")
    assert re.search(r"\.app-shell\s*\{[^}]*grid-template-columns:\s*13rem minmax\(0, 1fr\)", block)
    assert "px" not in block, "the wide-wall query only adjusts rem layout widths"


def test_no_px_sidebar_width(css: str) -> None:
    shell = declarations(top_level_rule(css, ".app-shell"))
    assert shell["grid-template-columns"] == "11.75rem minmax(0, 1fr)"
    assert "188px" not in css and "208px" not in css


def test_kids_grid_uses_auto_fit(css: str) -> None:
    grid = declarations(top_level_rule(css, ".kids-grid"))
    columns = grid["grid-template-columns"]
    assert "repeat(auto-fit, minmax(17.5rem, 1fr))" in columns
    assert "repeat(3, minmax(0, 1fr))" not in columns, "the fixed three-column grid is gone"
    assert grid["grid-gap"].endswith("rem")


def test_kids_grid_supports_kid_cols(css: str) -> None:
    pinned = top_level_rule(css, ".kids-grid[data-kid-cols]")
    assert "repeat(var(--kid-cols" in pinned
    assert "var(--kids-grid-cols" in declarations(top_level_rule(css, ".kids-grid"))["grid-template-columns"]


def test_ticker_height_in_rem(css: str) -> None:
    ticker = declarations(top_level_rule(css, ".news-ticker"))
    assert ticker["height"] == "2.2rem"
    assert ticker["overflow"] == "hidden"
    assert (ticker["display"], ticker["align-items"], ticker["position"]) == ("flex", "center", "relative")


def test_ticker_scrolls_on_transform_only(css: str) -> None:
    track = declarations(top_level_rule(css, ".news-ticker__track"))
    assert track["animation"].startswith("tickerScroll") and "linear infinite" in track["animation"]
    keyframes = block_after(css, "@keyframes tickerScroll")
    assert "translate3d(-100%, 0, 0)" in keyframes
    assert not re.search(r"\b(left|width|margin)\s*:", keyframes), "only transform animates (WebView 83 compositor)"


def test_ticker_honours_reduced_motion(css: str) -> None:
    block = block_after(css, "@media (prefers-reduced-motion: reduce)")
    assert ".news-ticker__track" in block
    track = declarations(block_after(block, ".news-ticker__track"))
    assert track["animation"] == "none"
    assert track["padding-left"] == "0", "the lead-in gap goes too, or the text would sit off-screen"


def test_card_padding_in_rem(css: str) -> None:
    for selector in (".panel", ".kid-head", ".meal-card", ".grocery-group", ".weather-card"):
        padding = declarations(top_level_rule(css, selector))["padding"]
        assert "px" not in padding and "rem" in padding, (selector, padding)


def test_sidebar_and_main_gutters_in_rem(css: str) -> None:
    assert "px" not in declarations(top_level_rule(css, ".sidebar"))["padding"]
    main = declarations(top_level_rule(css, "main"))
    assert "px" not in main["padding"] and main["max-width"].endswith("rem")


# ── index.html slots ──────────────────────────────────────────────────────────────────


def test_index_loads_app_as_module(html: str) -> None:
    assert re.search(r'<script type="module" src="/app\.mjs"></script>', html)


def test_index_has_ticker_and_weather_slots(html: str) -> None:
    assert re.search(r'<div id="news-ticker" class="news-ticker"[^>]*\bhidden\b', html)
    assert 'id="sidebar-weather"' in html
    assert html.index('id="notes"') < html.index('id="news-ticker"') < html.index("<main>")


# ── app.mjs wiring ────────────────────────────────────────────────────────────────────


def test_app_imports_display_module(mjs: str) -> None:
    match = re.search(r'^import \{([^}]*)\} from "\./display\.mjs";', mjs, re.MULTILINE)
    assert match
    names = {name.strip() for name in match.group(1).split(",")}
    assert {"resolveDisplay", "applyScale", "saveDisplay", "loadDisplay", "renderDisplayCard", "deviceInfo"} <= names


def test_app_applies_scale_before_first_view(mjs: str) -> None:
    boot = mjs.index('showView(location.hash.slice(1) || "chores", false);')
    cache_loaded = mjs.rindex("} catch (error) { state = null; }")
    assert "applyDisplay(resolveWallDisplay());" in mjs[cache_loaded:boot]
    apply = block_after(mjs, "function applyDisplay(")
    assert "applyScale(resolved.scale)" in apply


def test_settings_mounts_display_card(mjs: str) -> None:
    assert 'id="display-settings"' in block_after(mjs, "function renderSettings(")
    assert 'if (currentView === "settings") mountDisplayCard();' in mjs
    assert "renderDisplayCard(container" in block_after(mjs, "function mountDisplayCard(")


def test_auto_preset_refits_on_resize(mjs: str) -> None:
    watch = block_after(mjs, "function watchAutoFit(")
    assert '"resize"' in watch and '"orientationchange"' in watch
    refit = block_after(mjs, "function refitDisplay(")
    assert 'display.preset !== "auto"' in refit and "autoFitScale(" in refit


def test_saving_a_choice_persists_and_applies(mjs: str) -> None:
    save = block_after(mjs, "function saveWallDisplay(")
    assert "saveDisplay(choice)" in save and "applyDisplay(" in save

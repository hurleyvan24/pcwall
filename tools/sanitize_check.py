#!/usr/bin/env python3
"""Sanitization gate: keep private household details out of the published tree.

Run ``python tools/sanitize_check.py`` from the repository root. CI runs it on
every push and pull request. The gate scans every tracked file (or every source
file when git is unavailable) and reports each denylist hit with its path, line
number and offending token. Exit status 1 means the tree is not publishable.

What is flagged
---------------
* personal names, as whole words in any letter case;
* identifier-shaped names only as title-cased standalone words or lowercase
  quoted string values; programming identifiers and quoted object keys are allowed;
* household and network identifiers (school, host names, speaker ids and the
  private LAN prefix);
* the kids' birthdates;
* decimal coordinate pairs, unless the line carries ``# sample-coords`` or the
  pair is one of the public sample locations used by the weather tests;
* the names of private secrets, plus well-known secret shapes (API keys, OAuth
  tokens, private key blocks);
* files that must never be tracked (``.env``, ``credentials.json``,
  ``*.pickle``, ``data/*.json`` and OAuth token caches).

Private terms belong in the gitignored ``config/sanitize_denylist.json`` at the
repository root. It is a UTF-8 JSON object with any subset of ``names``,
``identifier_names``, ``household``, ``birthdays`` and ``secrets``, each containing
a list of non-empty strings. A UTF-8 BOM is accepted. Nothing is loaded at import.
The path comes from ``--denylist PATH``, then a non-empty
``PCWALL_SANITIZE_DENYLIST`` environment variable, then the default file.
Missing files leave only generic rules active; ``--require-private`` rejects
that state before publishing. ``--show-denylist`` prints the loaded categories.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "COORDINATE_PATTERN",
    "FORBIDDEN_FILES",
    "Hit",
    "PUBLIC_SAMPLE_COORDINATES",
    "SAMPLE_COORDS_MARKER",
    "SECRET_PATTERNS",
    "SKIP_DIRS",
    "build_denylist",
    "compile_rules",
    "default_denylist_path",
    "load_private_terms",
    "main",
    "scan_file",
    "scan_lines",
    "scan_tree",
]

#: A line containing this marker may hold coordinates (documentation samples).
SAMPLE_COORDS_MARKER = "# sample-coords"

#: Public landmark coordinates (Washington, DC) that the weather tests use as
#: their sample location. They are not a household location. Compared after
#: whitespace is removed from the matched pair.
PUBLIC_SAMPLE_COORDINATES = frozenset({"38.8951,-77.0364", "38.89511,-77.03637"})  # sample-coords

#: Directories skipped when walking without git.
SKIP_DIRS = frozenset(
    {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)

#: Decimal coordinate pair such as ``12.3456, -123.4567``.  # sample-coords
COORDINATE_PATTERN = r"-?\d{2}\.\d{3,},\s*-?\d{2,3}\.\d{3,}"

#: Shapes of well-known credentials. Matched case-sensitively as written.
SECRET_PATTERNS: tuple[str, ...] = (
    r"AIza[0-9A-Za-z_\-]{35}",  # Google API key
    r"GOCSPX-[0-9A-Za-z_\-]{20,}",  # Google OAuth client secret
    r"ya29\.[0-9A-Za-z_\-]{20,}",  # Google OAuth access token
    r"(?<![0-9A-Za-z_])1//0[0-9A-Za-z_\-]{20,}",  # Google OAuth refresh token
    r"gh[pousr]_[A-Za-z0-9]{36,}",  # GitHub token
    r"AKIA[0-9A-Z]{16}",  # AWS access key id
    r"xox[baprs]-[0-9A-Za-z\-]{10,}",  # Slack token
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----",  # PEM private key block
)

#: Globs of files that must never be part of the published tree. Globs without a
#: slash match the file name anywhere in the tree; globs with a slash match the
#: path relative to the scanned root.
FORBIDDEN_FILES: tuple[str, ...] = (
    ".env",
    "credentials.json",
    "*.pickle",
    "data/*.json",
    "token.json",
    "*.token.json",
    "config/sanitize_denylist.json",
)

_PRIVATE_CATEGORIES = ("names", "identifier_names", "household", "birthdays", "secrets")
_LITERAL_CATEGORIES = frozenset({"names", "household", "birthdays", "secrets"})
_IDENT = "A-Za-z0-9_"
_SPEC_ALLOWED_AFTER = re.compile(r"[(_\-]|item", re.IGNORECASE)
_QUOTED_KEY_AFTER = re.compile(r"""["']\s*:""")
_Finder = Callable[[str], list[str]]


def default_denylist_path() -> Path:
    """Return the repository-local private term file, independently of cwd."""
    return Path(__file__).resolve().parents[1] / "config" / "sanitize_denylist.json"


def _private_terms_path(path: Path | str | None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get("PCWALL_SANITIZE_DENYLIST") or default_denylist_path())


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path(__file__).resolve().parents[1]).as_posix()
    except ValueError:
        return str(resolved)


def _read_private_terms(path: Path) -> dict[str, list[str]] | None:
    """Validate one read; None distinguishes a missing file from an empty list."""
    label = _display_path(path)
    if path.is_dir():
        raise ValueError(f"{label}: $: must be a file, not a directory")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except UnicodeError:
        raise ValueError(f"{label}: $: must be UTF-8 JSON") from None
    except OSError:
        raise ValueError(f"{label}: $: cannot read private term file") from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{label}: $: malformed JSON (line {exc.lineno}, column {exc.colno})"
        ) from None
    if not isinstance(document, dict):
        raise ValueError(f"{label}: $: must be a JSON object")
    private = {category: [] for category in _PRIVATE_CATEGORIES}
    for category, entries in document.items():
        if category not in private:
            # An unrecognised key could itself contain private text.
            raise ValueError(f"{label}: $: unknown key")
        if not isinstance(entries, list):
            raise ValueError(f"{label}: {category}: must be a list")
        for index, entry in enumerate(entries):
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(f"{label}: {category}[{index}]: must be a non-empty string")
        private[category] = entries
    return private


def load_private_terms(path: Path | str | None = None) -> dict[str, list[str]]:
    """Load validated private terms, returning all five categories even if absent."""
    private = _read_private_terms(_private_terms_path(path))
    return private if private is not None else {category: [] for category in _PRIVATE_CATEGORIES}


def build_denylist(private: dict[str, list[str]]) -> dict[str, list[str]]:
    """Combine copies of the private categories with the generic publishing rules."""
    return {
        **{category: list(private.get(category, [])) for category in _PRIVATE_CATEGORIES},
        "coordinates": [COORDINATE_PATTERN],
        "secret_patterns": list(SECRET_PATTERNS),
        "forbidden_files": list(FORBIDDEN_FILES),
    }


@dataclass(frozen=True)
class Hit:
    """One denylist match: where it is and what matched."""

    path: str
    line: int
    word: str
    context: str


def _is_ident(char: str) -> bool:
    return char.isalnum() or char == "_"


def _literal_pattern(term: str) -> re.Pattern[str]:
    """Case-insensitive whole-term match; internal whitespace is flexible."""
    body = r"\s+".join(re.escape(word) for word in term.split())
    lead = rf"(?<![{_IDENT}])" if _is_ident(term[0]) else ""
    if term.endswith("."):
        trail = r"\d*"  # numeric prefix such as a LAN subnet: capture the rest of the address
    elif _is_ident(term[-1]):
        trail = rf"(?![{_IDENT}])"
    else:
        trail = ""
    return re.compile(lead + body + trail, re.IGNORECASE)


def _finder_for_literal(term: str) -> _Finder:
    pattern = _literal_pattern(term)
    return lambda line: [match.group(0) for match in pattern.finditer(line)]


def _finder_for_regex(source: str) -> _Finder:
    pattern = re.compile(source)
    return lambda line: [match.group(0) for match in pattern.finditer(line)]


def _finder_for_coordinates(source: str) -> _Finder:
    pattern = re.compile(source)

    def find(line: str) -> list[str]:
        if SAMPLE_COORDS_MARKER in line:
            return []
        return [
            match.group(0)
            for match in pattern.finditer(line)
            if re.sub(r"\s+", "", match.group(0)) not in PUBLIC_SAMPLE_COORDINATES
        ]

    return find


def _finder_for_builtin_shaped_name(term: str) -> _Finder:
    """Flag an identifier-shaped name only where it reads as a person's name."""
    term = term.lower()
    pattern = re.compile(rf"(?<![{_IDENT}]){re.escape(term)}", re.IGNORECASE)
    title = term.capitalize()

    def find(line: str) -> list[str]:
        found: list[str] = []
        for match in pattern.finditer(line):
            token = match.group(0)
            before = line[: match.start()]
            after = line[match.end() :]
            if _SPEC_ALLOWED_AFTER.match(after) or after[:1].isalnum():
                continue  # function calls, underscore/hyphen suffixes, longer words
            if before.endswith("."):
                continue  # member access
            quote = before[-1:] if before[-1:] in ('"', "'") else ""
            if quote and _QUOTED_KEY_AFTER.match(after):
                continue  # quoted object key
            quoted_value = bool(quote) and after[:1] == quote
            if token == title or (token == term and quoted_value):
                found.append(token)
        return found

    return find


def compile_rules(denylist: dict[str, Iterable[str]]) -> tuple[list[_Finder], list[str]]:
    """Turn a denylist dict into content finders and forbidden-file globs."""
    finders: list[_Finder] = []
    globs: list[str] = []
    identifier_names = dict.fromkeys(entry.lower() for entry in denylist.get("identifier_names", ()))
    for category, entries in denylist.items():
        if category == "identifier_names":
            finders.extend(_finder_for_builtin_shaped_name(entry) for entry in identifier_names)
            continue
        if category not in _LITERAL_CATEGORIES | {"coordinates", "secret_patterns", "forbidden_files"}:
            raise ValueError(f"unknown denylist category: {category!r}")
        for entry in entries:
            if category in _LITERAL_CATEGORIES:
                if category == "names" and entry.lower() in identifier_names:
                    continue
                finders.append(_finder_for_literal(entry))
            elif category == "coordinates":
                finders.append(_finder_for_coordinates(entry))
            elif category == "secret_patterns":
                finders.append(_finder_for_regex(entry))
            elif category == "forbidden_files":
                globs.append(entry)
    return finders, globs


def _context(line: str, limit: int = 160) -> str:
    text = line.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _scan_lines(lines: Iterable[str], path: str, finders: list[_Finder]) -> list[Hit]:
    hits: list[Hit] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r\n")
        for find in finders:
            for token in find(line):
                hits.append(Hit(path, number, token, _context(line)))
    return hits


def _matches_glob(rel: str, glob: str) -> bool:
    if "/" in glob:
        return fnmatch.fnmatchcase(rel, glob)
    return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], glob)


def _scan_file(file: Path, rel: str, finders: list[_Finder], globs: list[str]) -> list[Hit]:
    hits = [Hit(rel, 0, glob, "forbidden file is part of the tree") for glob in globs if _matches_glob(rel, glob)]
    if not file.is_file():
        return hits
    data = file.read_bytes()
    if b"\0" in data[:8192]:
        return hits  # binary content is not scanned
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    hits.extend(_scan_lines(lines, rel, finders))
    return hits


def _git_ls_files(base: Path) -> list[str] | None:
    try:
        proc = subprocess.run(["git", "-C", str(base), "ls-files", "-z"], capture_output=True, check=False)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return sorted(entry for entry in proc.stdout.decode("utf-8", errors="replace").split("\0") if entry)


def _walk_files(base: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            found.append(Path(dirpath, name).relative_to(base).as_posix())
    return sorted(found)


def _tree_files(base: Path, use_git: bool | None) -> list[str]:
    if use_git is None:
        use_git = (base / ".git").exists()
    if use_git:
        listed = _git_ls_files(base)
        if listed is not None:
            return listed
    return _walk_files(base)


def scan_lines(lines: Iterable[str], path: str, denylist: dict[str, Iterable[str]] | None = None) -> list[Hit]:
    """Scan already-read lines, reporting hits against ``path``."""
    finders, _ = compile_rules(build_denylist(load_private_terms()) if denylist is None else denylist)
    return _scan_lines(lines, path, finders)


def scan_file(
    path: Path | str, denylist: dict[str, Iterable[str]] | None = None, rel: str | None = None
) -> list[Hit]:
    """Scan one file. ``rel`` is the path reported in hits (default: file name)."""
    file = Path(path)
    finders, globs = compile_rules(build_denylist(load_private_terms()) if denylist is None else denylist)
    return _scan_file(file, rel or file.name, finders, globs)


def scan_tree(
    root: Path | str = ".",
    denylist: dict[str, Iterable[str]] | None = None,
    *,
    use_git: bool | None = None,
) -> list[Hit]:
    """Scan every tracked file under ``root`` (every source file without git).

    Git is used when ``root`` holds a ``.git`` entry and ``git ls-files`` works;
    otherwise the directory is walked, skipping :data:`SKIP_DIRS`. Pass
    ``use_git`` to force either mode.
    """
    base = Path(root).resolve()
    finders, globs = compile_rules(build_denylist(load_private_terms()) if denylist is None else denylist)
    hits: list[Hit] = []
    for rel in _tree_files(base, use_git):
        hits.extend(_scan_file(base / rel, rel, finders, globs))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sanitize_check",
        description="Fail when private household details or secrets are present in the tree.",
    )
    parser.add_argument("root", nargs="?", default=".", help="directory to scan (default: current directory)")
    parser.add_argument("--no-git", action="store_true", help="walk the directory even inside a git checkout")
    parser.add_argument("--denylist", type=Path, help="private term JSON file (overrides environment and default)")
    parser.add_argument("--require-private", action="store_true", help="fail if the private term file is missing")
    parser.add_argument(
        "--show-denylist", action="store_true", help="print the loaded denylist and exit (local use only)"
    )
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    path = _private_terms_path(args.denylist)
    try:
        private = _read_private_terms(path)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 2
    if private is None:
        label = _display_path(path)
        if args.require_private:
            print(f"FAIL: no private term list at {label}; create it before publishing.")
            return 2
        print(f"NOTE: no private term list at {label}; checked generic rules only.")
        private = {category: [] for category in _PRIVATE_CATEGORIES}
    denylist = build_denylist(private)

    if args.show_denylist:
        for category, entries in denylist.items():
            print(f"{category}:")
            for entry in entries:
                print(f"  {entry}")
        return 0

    hits = scan_tree(args.root, denylist=denylist, use_git=False if args.no_git else None)
    for hit in hits:
        print(f"FAIL: {hit.path}:{hit.line} hit=[{hit.word}] context=[{hit.context}]")
    if hits:
        print(f"FAIL: {len(hits)} sanitization hit(s); the tree is not publishable.")
        return 1
    print("PASS: Sanitization clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

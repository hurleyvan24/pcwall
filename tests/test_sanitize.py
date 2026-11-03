"""Publishing gate tests use only synthetic terms and isolated private files."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import sanitize_check as sc  # noqa: E402


CATEGORIES = ("names", "identifier_names", "household", "birthdays", "secrets")
SYNTHETIC = {
    "names": ["zorblat"],
    "identifier_names": ["min", "quux"],
    "household": ["pretend moon academy", "pretend-host", "10.20.30."],
    "birthdays": ["2099-12-31"],
    "secrets": ["pretend_launch_key"],
}


@pytest.fixture(autouse=True)
def isolate_private_list(monkeypatch, tmp_path: Path) -> None:
    # No test may consult a developer's actual private file, even indirectly.
    monkeypatch.setenv("PCWALL_SANITIZE_DENYLIST", str(tmp_path / "absent-private.json"))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    path = tmp_path / "tree"
    path.mkdir()
    return path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _json_file(tmp_path: Path, document, name: str = "private.json") -> Path:
    return _write(tmp_path / name, json.dumps(document))


def _words(hits: list[sc.Hit]) -> list[str]:
    return [hit.word for hit in hits]


def test_sanitize_catches_names(tree: Path) -> None:
    _write(tree / "notes.md", "Zorblat packed the lunches.\nzorblat ZORBLAT\n")
    assert sc.scan_tree(tree, denylist=sc.build_denylist(SYNTHETIC)) == [
        sc.Hit("notes.md", 1, "Zorblat", "Zorblat packed the lunches."),
        sc.Hit("notes.md", 2, "zorblat", "zorblat ZORBLAT"),
        sc.Hit("notes.md", 2, "ZORBLAT", "zorblat ZORBLAT"),
    ]


@pytest.mark.parametrize("text", ["zorblattown", "azORBLAT", "_zorblat", "zorblat_", "zorblat2"])
def test_names_require_word_boundaries(text: str) -> None:
    assert sc.scan_lines([text], "probe", {"names": ["zorblat"]}) == []


def test_literals_escape_regex_and_allow_flexible_whitespace() -> None:
    rules = {"household": ["pretend moon academy", "fake[host]+", "pretend.host"]}
    line = "PRETEND\t MOON   ACADEMY; fake[host]+; pretend.host"
    assert _words(sc.scan_lines([line], "probe", rules)) == [
        "PRETEND\t MOON   ACADEMY", "fake[host]+", "pretend.host",
    ]
    assert sc.scan_lines(["pretendXhost fakeh"], "probe", rules) == []


@pytest.mark.parametrize("term", ["min", "quux", "len"])
@pytest.mark.parametrize("template", [
    "best = {term}(1, 2)", "{term}_items = 3", ".card {{ {term}-width: 12rem; }}",
    "slider.{term} = String(grid.{term});", "let {term} = 1;", "{upper} = 10",
    '{{"{term}": 5}}', "{{'{term}' : 5}}", '{{"{title}": 5}}',
    "{term}item {term}items {term}town {term}2 _{term} other{term}",
    "{title}_items {title}-width {title}(1) grid.{title}", "'{upper}'", "'{term}\"",
])
def test_identifier_names_allow_programming_shapes(term: str, template: str) -> None:
    line = template.format(term=term, upper=term.upper(), title=term.title())
    assert sc.scan_lines([line], "probe", {"identifier_names": [term]}) == []


@pytest.mark.parametrize("term", ["min", "quux", "len"])
def test_identifier_names_catch_values_and_title_words(term: str) -> None:
    lines = [f'"id": "{term}"', f"{term.title()} goes to bed at eight.", f"'{term}'", f"({term.title()})."]
    hits = sc.scan_lines(lines, "probe", {"identifier_names": [term.upper()]})
    assert [(hit.line, hit.word) for hit in hits] == [(1, term), (2, term.title()), (3, term), (4, term.title())]


@pytest.mark.parametrize("reverse", [False, True])
def test_identifier_names_override_names_without_double_hits(reverse: bool) -> None:
    entries = [("names", ["MIN", "min", "zorblat"]), ("identifier_names", iter(["Min", "min", "quux"]))]
    rules = dict(reversed(entries) if reverse else entries)
    hits = sc.scan_lines(['Min said "min"; quux(1); "quux"; min(1); let min = 1;'], "probe", rules)
    assert _words(hits) == ["Min", "min", "quux"]
    assert _words(sc.scan_lines(["let min = 1;"], "probe", {"names": ["min"]})) == ["min"]


def test_sanitize_catches_private_ips_and_household_terms() -> None:
    hits = sc.scan_lines(
        ["speaker at 10.20.30.42", "router at 192.168.1.1", "PRETEND-HOST", "PRETEND MOON ACADEMY"],
        "network", sc.build_denylist(SYNTHETIC),
    )
    assert [(hit.line, hit.word) for hit in hits] == [
        (1, "10.20.30.42"), (3, "PRETEND-HOST"), (4, "PRETEND MOON ACADEMY"),
    ]
    assert sc.scan_lines(["110.20.30.42 otherpretend-host pretend-host2"], "probe", sc.build_denylist(SYNTHETIC)) == []


def test_sanitize_catches_birthdays_and_secret_names() -> None:
    hits = sc.scan_lines(
        ['{"birthdate": "2099-12-31"}', '"2098-12-31"', "PRETEND_LAUNCH_KEY=", "pretend_launch_key_suffix="],
        "settings", sc.build_denylist(SYNTHETIC),
    )
    assert [(hit.line, hit.word) for hit in hits] == [(1, "2099-12-31"), (3, "PRETEND_LAUNCH_KEY")]


SECRET_SHAPES = [
    "AIza" + "A" * 35, "GOCSPX-" + "a_" * 10, "ya29." + "a-" * 10, "1//0" + "a_" * 10,
    *["gh" + kind + "_" + "x" * 36 for kind in "pousr"], "AKIA" + "A1" * 8,
    *["xox" + kind + "-" + "ab-12" * 2 for kind in "baprs"],
    "-----BEGIN " + "PRIVATE KEY-----", "-----BEGIN RSA " + "PRIVATE KEY-----",
]


@pytest.mark.parametrize("token", SECRET_SHAPES)
def test_generic_secret_shapes(token: str) -> None:
    assert _words(sc.scan_lines([f"key={token}"], "probe", sc.build_denylist({}))) == [token]


@pytest.mark.parametrize("text", [
    "AIza" + "A" * 34, "GOCSPX-" + "a" * 19, "ya29." + "a" * 19,
    "1//0" + "a" * 19, "a1//0" + "a" * 20, "_1//0" + "a" * 20,
    "ghp_" + "x" * 35, "AKIA" + "A" * 15, "xoxb-" + "a" * 9,
    "aiza" + "A" * 35, "-----BEGIN " + "PUBLIC KEY-----", "PCWALL_KEY=",
])
def test_generic_secret_near_misses(text: str) -> None:
    assert sc.scan_lines([text], "probe", sc.build_denylist({})) == []


def test_sanitize_flags_coordinates_unless_marked() -> None:
    pair = "37.421998, -122.084000"  # sample-coords
    rules = sc.build_denylist({})
    assert _words(sc.scan_lines([f"HOME = ({pair})"], "weather", rules)) == [pair]
    assert sc.scan_lines([f"HOME = ({pair}) {sc.SAMPLE_COORDS_MARKER}"], "weather", rules) == []
    assert _words(sc.scan_lines([pair, sc.SAMPLE_COORDS_MARKER], "weather", rules)) == [pair]
    for public in sc.PUBLIC_SAMPLE_COORDINATES:
        assert sc.scan_lines([public.replace(",", ", \t")], "weather", rules) == []
    assert sc.scan_lines(["12.34, -123.45"], "weather", rules) == []


@pytest.mark.parametrize(("rel", "glob"), [
    (".env", ".env"), ("nested/.env", ".env"),
    ("credentials.json", "credentials.json"), ("nested/credentials.json", "credentials.json"),
    ("cache.pickle", "*.pickle"), ("data/wall.json", "data/*.json"),
    ("token.json", "token.json"), ("nested/oauth.token.json", "*.token.json"),
    ("config/sanitize_denylist.json", "config/sanitize_denylist.json"),
])
def test_sanitize_flags_forbidden_files(tree: Path, rel: str, glob: str) -> None:
    _write(tree / rel, "{}\n")
    assert sc.scan_tree(tree) == [sc.Hit(rel, 0, glob, "forbidden file is part of the tree")]


def test_forbidden_files_do_not_overmatch(tree: Path) -> None:
    for rel in (".env.example", "config/x.json", "data/wall.txt", "credentials.json.example", "token.json.example"):
        _write(tree / rel, "{}\n")
    assert sc.scan_tree(tree) == []


def test_build_denylist_copies_terms_and_keeps_generic_rules() -> None:
    private = {"names": ["zorblat"]}
    result = sc.build_denylist(private)
    assert set(result) == set(CATEGORIES) | {"coordinates", "secret_patterns", "forbidden_files"}
    assert result["coordinates"] == [sc.COORDINATE_PATTERN]
    assert result["secret_patterns"] == list(sc.SECRET_PATTERNS)
    assert result["forbidden_files"] == list(sc.FORBIDDEN_FILES)
    result["names"].append("quux")
    result["secret_patterns"].clear()
    result["household"].append("pretend-host")
    assert private == {"names": ["zorblat"]}
    assert sc.build_denylist({})["secret_patterns"] == list(sc.SECRET_PATTERNS)
    assert sc.build_denylist({})["household"] == []


def test_default_path_ignores_cwd_and_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    assert sc.default_denylist_path() == ROOT / "config" / "sanitize_denylist.json"
    assert {"default_denylist_path", "load_private_terms", "build_denylist"} <= set(sc.__all__)
    for removed in ("DENYLIST", "BUILTIN_SHAPED_NAME"):
        assert removed not in vars(sc) and removed not in sc.__all__


def test_loader_missing_returns_independent_empty_categories(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    first = sc.load_private_terms(missing)
    assert first == dict.fromkeys(CATEGORIES, [])
    first["names"].append("zorblat")
    assert first["household"] == []
    assert sc.load_private_terms(str(missing)) == dict.fromkeys(CATEGORIES, [])
    assert not missing.exists()


@pytest.mark.parametrize("document", [{}, {"names": ["zorblat"]}, SYNTHETIC])
def test_loader_valid_subsets(tmp_path: Path, document: dict) -> None:
    result = sc.load_private_terms(_json_file(tmp_path, document))
    assert result == {category: document.get(category, []) for category in CATEGORIES}


def test_loader_accepts_bom_crlf_and_unicode(tmp_path: Path) -> None:
    path = tmp_path / "private.json"
    path.write_bytes('\ufeff{\r\n  "names": ["zörblät"]\r\n}\r\n'.encode("utf-8"))
    assert sc.load_private_terms(path)["names"] == ["zörblät"]


@pytest.mark.parametrize("env", [None, ""])
def test_loader_unset_or_empty_env_uses_default(monkeypatch, tmp_path: Path, env) -> None:
    path = _json_file(tmp_path, SYNTHETIC)
    monkeypatch.setattr(sc, "default_denylist_path", lambda: path)
    if env is None:
        monkeypatch.delenv("PCWALL_SANITIZE_DENYLIST")
    else:
        monkeypatch.setenv("PCWALL_SANITIZE_DENYLIST", env)
    assert sc.load_private_terms() == SYNTHETIC


def test_loader_explicit_path_beats_env_which_beats_default(monkeypatch, tmp_path: Path) -> None:
    default = _write(tmp_path / "default.json", "invalid")
    env = _json_file(tmp_path, {"names": ["zorblat"]}, "env.json")
    explicit = _json_file(tmp_path, {"names": ["quux"]}, "explicit.json")
    monkeypatch.setattr(sc, "default_denylist_path", lambda: default)
    monkeypatch.setenv("PCWALL_SANITIZE_DENYLIST", str(env))
    assert sc.load_private_terms()["names"] == ["zorblat"]
    assert sc.load_private_terms(str(explicit))["names"] == ["quux"]
    assert sc.load_private_terms(tmp_path / "missing.json") == dict.fromkeys(CATEGORIES, [])


INVALID_DOCUMENTS = [
    *[(raw, "$: must be a JSON object") for raw in ("[]", "null", "42", "true", '"synthetic-sensitive"')],
    *[(json.dumps({key: []}), "$: unknown key") for key in ("synthetic-sensitive", "coordinates", "secret_patterns", "forbidden_files")],
    *[(json.dumps({category: "synthetic-sensitive"}), f"{category}: must be a list") for category in CATEGORIES],
    *[(json.dumps({"names": value}), "names: must be a list") for value in (None, 1, True, {})],
    *[
        (json.dumps({category: ["synthetic-sensitive", value]}), f"{category}[1]: must be a non-empty string")
        for category in CATEGORIES for value in (None, 1, True, {}, [], "", " \t\r\n", "\u2003")
    ],
]


@pytest.mark.parametrize(("raw", "message"), INVALID_DOCUMENTS)
def test_loader_errors_name_file_and_json_path_without_values(tmp_path: Path, raw: str, message: str) -> None:
    path = _write(tmp_path / "sanitize_denylist.json", raw)
    with pytest.raises(ValueError) as exc:
        sc.load_private_terms(path)
    assert str(exc.value) == f"{path}: {message}"
    assert "synthetic-sensitive" not in str(exc.value)


@pytest.mark.parametrize("raw", ["", "{", '{"names": ["synthetic-sensitive"],}', '{"names": ["synthetic-sensitive"]} trailing'])
def test_loader_redacts_malformed_json(tmp_path: Path, raw: str) -> None:
    path = _write(tmp_path / "private.json", raw)
    with pytest.raises(ValueError) as exc:
        sc.load_private_terms(path)
    assert str(exc.value).startswith(f"{path}: $: malformed JSON (line ")
    assert "synthetic-sensitive" not in str(exc.value)
    assert exc.value.__suppress_context__


def test_loader_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc:
        sc.load_private_terms(tmp_path)
    assert str(exc.value) == f"{tmp_path}: $: must be a file, not a directory"


def test_loader_redacts_invalid_utf8_and_read_errors(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "private.json"
    path.write_bytes(b'{"names": ["synthetic-sensitive\xff"]}')
    with pytest.raises(ValueError) as exc:
        sc.load_private_terms(path)
    assert str(exc.value) == f"{path}: $: must be UTF-8 JSON"

    def unreadable(*args, **kwargs):
        raise PermissionError("synthetic-sensitive")

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(ValueError) as exc:
        sc.load_private_terms(path)
    assert str(exc.value) == f"{path}: $: cannot read private term file"


def test_import_with_invalid_default_file_is_lazy(monkeypatch, tmp_path: Path) -> None:
    _write(tmp_path / "config" / "sanitize_denylist.json", "invalid synthetic-sensitive")
    monkeypatch.delenv("PCWALL_SANITIZE_DENYLIST")
    name = "_isolated_sanitizer_test"
    spec = importlib.util.spec_from_file_location(name, TOOLS / "sanitize_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Execute the actual code with an isolated default path, without copying it.
    module.__file__ = str(tmp_path / "tools" / "sanitize_check.py")
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    assert module.default_denylist_path() == tmp_path / "config" / "sanitize_denylist.json"
    with pytest.raises(ValueError, match="malformed JSON"):
        module.load_private_terms()


@pytest.mark.parametrize("api", ["lines", "file", "tree"])
def test_scan_apis_load_on_call_and_explicit_rules_bypass_loading(monkeypatch, tmp_path: Path, tree: Path, api: str) -> None:
    file = _write(tree / "probe.txt", "Zorblat\n")
    private = _json_file(tmp_path, {"names": ["zorblat"]})

    def scan(**kwargs):
        if api == "lines":
            return sc.scan_lines(["Zorblat"], "probe.txt", **kwargs)
        if api == "file":
            return sc.scan_file(file, **kwargs)
        return sc.scan_tree(tree, **kwargs)

    assert scan() == []
    monkeypatch.setenv("PCWALL_SANITIZE_DENYLIST", str(private))
    assert _words(scan()) == ["Zorblat"]
    invalid = _write(tmp_path / "invalid.json", "invalid")
    monkeypatch.setenv("PCWALL_SANITIZE_DENYLIST", str(invalid))
    assert scan(denylist={}) == []
    with pytest.raises(ValueError, match="malformed JSON"):
        scan()


def test_sanitize_main_reports_and_exits(tmp_path: Path, tree: Path, capsys) -> None:
    private = _json_file(tmp_path, {"birthdays": ["2099-12-31"]})
    _write(tree / "notes.md", "born 2099-12-31\n")
    assert sc.main([str(tree), "--denylist", str(private)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "FAIL: notes.md:1 hit=[2099-12-31] context=[born 2099-12-31]",
        "FAIL: 1 sanitization hit(s); the tree is not publishable.",
    ]


@pytest.mark.parametrize("require", [False, True])
def test_main_loaded_clean_list_has_no_note(tmp_path: Path, tree: Path, capsys, require: bool) -> None:
    private = _json_file(tmp_path, {})
    args = [str(tree), "--denylist", str(private)] + (["--require-private"] if require else [])
    assert sc.main(args) == 0
    assert capsys.readouterr().out == "PASS: Sanitization clean.\n"


@pytest.mark.parametrize("has_hit", [False, True])
def test_main_missing_list_notes_once_then_checks_generic_rules(monkeypatch, tmp_path: Path, tree: Path, capsys, has_hit: bool) -> None:
    monkeypatch.setattr(sc, "__file__", str(tmp_path / "tools" / "sanitize_check.py"))
    monkeypatch.setenv("PCWALL_SANITIZE_DENYLIST", "")
    if has_hit:
        _write(tree / ".env", "")
    assert sc.main([str(tree)]) == int(has_hit)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "NOTE: no private term list at config/sanitize_denylist.json; checked generic rules only."
    assert sum(line.startswith("NOTE:") for line in lines) == 1
    if has_hit:
        assert lines[1:] == [
            "FAIL: .env:0 hit=[.env] context=[forbidden file is part of the tree]",
            "FAIL: 1 sanitization hit(s); the tree is not publishable.",
        ]
    else:
        assert lines[1:] == ["PASS: Sanitization clean."]


@pytest.mark.parametrize("show", [False, True])
def test_main_requires_missing_file_before_scan(monkeypatch, tmp_path: Path, tree: Path, capsys, show: bool) -> None:
    def unexpected_scan(*args, **kwargs):
        pytest.fail("must not scan when private terms are required and absent")

    monkeypatch.setattr(sc, "scan_tree", unexpected_scan)
    path = tmp_path / "missing.json"
    args = [str(tree), "--require-private", "--denylist", str(path)] + (["--show-denylist"] if show else [])
    assert sc.main(args) == 2
    assert capsys.readouterr().out == f"FAIL: no private term list at {path}; create it before publishing.\n"


@pytest.mark.parametrize("kind", ["malformed", "object", "entry", "directory"])
def test_main_invalid_list_exits_two_without_values(tmp_path: Path, tree: Path, capsys, kind: str) -> None:
    if kind == "directory":
        path = tmp_path
    else:
        raw = {"malformed": "synthetic-sensitive", "object": "[]", "entry": '{"names": ["synthetic-sensitive", null]}'}[kind]
        path = _write(tmp_path / "private.json", raw)
    with pytest.raises(ValueError) as exc:
        sc.load_private_terms(path)
    assert sc.main([str(tree), "--denylist", str(path)]) == 2
    output = capsys.readouterr().out
    assert output == f"FAIL: {exc.value}\n"
    assert "synthetic-sensitive" not in output


def test_main_cli_overrides_invalid_env(monkeypatch, tmp_path: Path, tree: Path, capsys) -> None:
    invalid = _write(tmp_path / "invalid.json", "invalid")
    monkeypatch.setenv("PCWALL_SANITIZE_DENYLIST", str(invalid))
    private = _json_file(tmp_path, {"names": ["zorblat"]})
    _write(tree / "notes.md", "Zorblat\n")
    assert sc.main([str(tree), "--denylist", str(private)]) == 1
    assert "hit=[Zorblat]" in capsys.readouterr().out
    missing = tmp_path / "missing.json"
    assert sc.main([str(tree), "--denylist", str(missing)]) == 0
    assert capsys.readouterr().out == (
        f"NOTE: no private term list at {missing}; checked generic rules only.\nPASS: Sanitization clean.\n"
    )


def test_main_uses_env_and_shows_loaded_categories(monkeypatch, tmp_path: Path, capsys) -> None:
    private = _json_file(tmp_path, SYNTHETIC)
    monkeypatch.setenv("PCWALL_SANITIZE_DENYLIST", str(private))

    def unexpected_scan(*args, **kwargs):
        pytest.fail("--show-denylist must not scan")

    monkeypatch.setattr(sc, "scan_tree", unexpected_scan)
    assert sc.main(["--show-denylist", "--require-private"]) == 0
    expected = "".join(
        f"{category}:\n" + "".join(f"  {entry}\n" for entry in entries)
        for category, entries in sc.build_denylist(SYNTHETIC).items()
    )
    assert capsys.readouterr().out == expected


def test_cli_process_exit_code_and_output(tmp_path: Path, tree: Path) -> None:
    private = _json_file(tmp_path, {"names": ["zorblat"]})
    _write(tree / "notes.md", "Zorblat\n")
    result = subprocess.run(
        [sys.executable, "-B", str(TOOLS / "sanitize_check.py"), str(tree), "--denylist", str(private)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout.startswith("FAIL: notes.md:1 hit=[Zorblat] context=[Zorblat]\n")


def test_file_scanning_preserves_binary_and_text_behaviour(tree: Path) -> None:
    binary = tree / "cache.pickle"
    binary.write_bytes(b"\0Zorblat")
    assert sc.scan_file(binary, sc.build_denylist(SYNTHETIC)) == [
        sc.Hit("cache.pickle", 0, "*.pickle", "forbidden file is part of the tree"),
    ]
    file = tree / "notes.txt"
    file.write_bytes(b"clean\r\n Zorblat \xff\r\n")
    assert sc.scan_file(file, sc.build_denylist(SYNTHETIC), rel="nested/notes.txt") == [
        sc.Hit("nested/notes.txt", 2, "Zorblat", "Zorblat \ufffd"),
    ]
    assert sc.scan_file(tree / "missing.txt", denylist={}) == []
    assert sc.scan_file(tree / ".env", sc.build_denylist({}))[0].line == 0


def test_line_scanning_preserves_line_numbers_and_context_limit() -> None:
    line = "zorblat " + "x" * 180
    hits = sc.scan_lines(iter(["clean\r\n", f" {line}\r\n"]), "probe", {"names": ["zorblat"]})
    assert hits == [sc.Hit("probe", 2, "zorblat", line[:157] + "...")]


@pytest.mark.parametrize("directory", sorted(sc.SKIP_DIRS))
def test_walk_skips_generated_directories(tree: Path, directory: str) -> None:
    _write(tree / directory / "private.txt", "Zorblat")
    assert sc.scan_tree(tree, sc.build_denylist(SYNTHETIC), use_git=False) == []


def test_git_tracking_and_no_git_mode(monkeypatch, tree: Path, tmp_path: Path, capsys) -> None:
    _write(tree / ".git", "gitdir: unused")
    _write(tree / "tracked.txt", "clean")
    _write(tree / "untracked.txt", "Zorblat")
    _write(tree / "config" / "sanitize_denylist.json", "{}")
    monkeypatch.setattr(sc, "_git_ls_files", lambda root: ["tracked.txt"])
    rules = sc.build_denylist(SYNTHETIC)
    assert sc.scan_tree(tree, rules) == []
    assert sorted(hit.path for hit in sc.scan_tree(tree, rules, use_git=False)) == [
        "config/sanitize_denylist.json", "untracked.txt",
    ]
    private = _json_file(tmp_path, SYNTHETIC)
    assert sc.main([str(tree), "--denylist", str(private), "--no-git"]) == 1
    assert "hit=[Zorblat]" in capsys.readouterr().out
    monkeypatch.setattr(sc, "_git_ls_files", lambda root: ["config/sanitize_denylist.json"])
    assert sc.scan_tree(tree, rules)[0].word == "config/sanitize_denylist.json"
    monkeypatch.setattr(sc, "_git_ls_files", lambda root: None)
    assert len(sc.scan_tree(tree, rules, use_git=True)) == 2


@pytest.mark.parametrize("outcome", ["missing", "failure", "success"])
def test_git_file_listing_failure_and_sorted_paths(monkeypatch, tmp_path: Path, outcome: str) -> None:
    def run(command, **kwargs):
        assert command == ["git", "-C", str(tmp_path), "ls-files", "-z"]
        if outcome == "missing":
            raise FileNotFoundError
        return subprocess.CompletedProcess(command, int(outcome == "failure"), b"z.txt\0a.txt\0", b"")

    monkeypatch.setattr(sc.subprocess, "run", run)
    assert sc._git_ls_files(tmp_path) == (["a.txt", "z.txt"] if outcome == "success" else None)


def test_compile_rejects_unknown_categories() -> None:
    with pytest.raises(ValueError, match="unknown denylist category"):
        sc.compile_rules({"unknown": []})


def test_sanitize_passes_on_current_tree() -> None:
    hits = sc.scan_tree(ROOT)
    assert hits == [], hits
    own = [TOOLS / "sanitize_check.py", Path(__file__), ROOT / ".github/workflows/ci.yml", ROOT / "SECURITY.md"]
    assert [hit for path in own for hit in sc.scan_file(path, rel=path.name)] == []

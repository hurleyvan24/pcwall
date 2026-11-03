"""Focused tests for the publish wave: README, the service installers and the gate.

Both installers are exercised in dry-run mode only, so these tests never need
root, an elevated shell, NSSM or systemd. ``_posix`` spells a path the way the
``bash`` on PATH will see it: ``/mnt/<drive>/...`` for the WSL launcher that
lives in System32, ``/<drive>/...`` for Git Bash or MSYS, and unchanged on a
POSIX host. Nothing here writes outside ``tmp_path``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
PS1 = ROOT / "scripts" / "install_service.ps1"
SH = ROOT / "scripts" / "install_service.sh"
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import sanitize_check as sc  # noqa: E402

BASH = shutil.which("bash")
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
_DRIVE = re.compile(r"^([A-Za-z]):/(.*)$")
_WSL_LAUNCHER = re.compile(r"/(system32|sysnative|windowsapps)/bash(\.exe)?$")


def _is_wsl_bash(bash: str | None) -> bool:
    """True for the Windows ``bash.exe`` launcher that hands commands to WSL."""
    return bool(bash) and _WSL_LAUNCHER.search(str(bash).replace("\\", "/").lower()) is not None


def _posix(path: Path | str, bash: str | None = None) -> str:
    """Return *path* as the bash at *bash* (default: the one on PATH) expects it.

    ``F:\\repo\\x.sh`` becomes ``/mnt/f/repo/x.sh`` for WSL's System32 bash and
    ``/f/repo/x.sh`` for Git Bash or MSYS. A path without a drive letter only has
    its separators normalised, so the helper is a no-op on Linux and macOS.
    """
    text = str(path).replace("\\", "/")
    match = _DRIVE.match(text)
    if match is None:
        return text
    drive, rest = match.group(1).lower(), match.group(2)
    prefix = f"/mnt/{drive}" if _is_wsl_bash(BASH if bash is None else bash) else f"/{drive}"
    return f"{prefix}/{rest}"


def _run(command: list[str], timeout: float = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=ROOT
    )


def _bash_ready() -> bool:
    """A bash that cannot run ``echo`` (a WSL launcher without a distro) skips the tests."""
    if BASH is None:
        return False
    try:
        return _run([BASH, "-c", "echo ready"], timeout=60).stdout.strip() == "ready"
    except (OSError, subprocess.SubprocessError):
        return False


def _lf_copy(source: Path, directory: Path) -> Path:
    """Copy a script with LF endings so a ``core.autocrlf=true`` checkout still runs under bash."""
    target = directory / source.name
    target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    return target


def _missing(haystack: str, needles: list[str]) -> list[str]:
    """The expected fragments *haystack* lacks; an empty list means every one is present."""
    return [needle for needle in needles if needle not in haystack]


def _blockquote(text: str, start: str) -> str:
    """The markdown blockquote beginning with *start*, whitespace-normalised (wrapping is free)."""
    lines = text.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(start))
    quoted: list[str] = []
    while index < len(lines) and lines[index].startswith(">"):
        quoted.append(lines[index].lstrip(">").strip())
        index += 1
    return " ".join(" ".join(quoted).split())


# --- _posix ------------------------------------------------------------------------------


def test_posix_maps_drive_letters_for_each_bash() -> None:
    wsl = r"C:\WINDOWS\system32\bash.exe"
    git = r"C:\Program Files\Git\usr\bin\bash.exe"
    windows = r"F:\repo\scripts\install_service.sh"
    assert (_posix(windows, bash=wsl), _posix(windows, bash=git), _posix("D:/already/forward", bash=wsl)) == (
        "/mnt/f/repo/scripts/install_service.sh", "/f/repo/scripts/install_service.sh", "/mnt/d/already/forward"
    )
    assert (_posix("/home/wall/pcwall/scripts/x.sh", bash=wsl), _posix(Path("relative") / "file.sh", bash=None)) == (
        "/home/wall/pcwall/scripts/x.sh", "relative/file.sh"
    )


def test_posix_paths_resolve_inside_the_available_bash() -> None:
    if not _bash_ready():
        pytest.skip("no working bash on PATH")
    proc = _run([BASH, "-c", f'test -f "{_posix(SH)}" && test -d "{_posix(ROOT)}" && echo found'])
    assert proc.stdout.strip() == "found", (BASH, _posix(SH), proc.stderr)


# --- scripts/install_service.sh ----------------------------------------------------------


def test_install_service_sh_dry_run_renders_the_unit(tmp_path: Path) -> None:
    if not _bash_ready():
        pytest.skip("no working bash on PATH")
    script = _lf_copy(SH, tmp_path)
    root = tmp_path / "wall"
    root.mkdir()
    assert _run([BASH, "-n", _posix(script)]).returncode == 0

    proc = _run(
        [BASH, _posix(script), "--dry-run", "--root", _posix(root), "--user", "wall",
         "--python", "/opt/wall/.venv/bin/python"]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    unit = [
        "/etc/systemd/system/pcwall.service", "Restart=always", "User=wall",
        f"WorkingDirectory={_posix(root)}", f"EnvironmentFile=-{_posix(root)}/.env",
        "ExecStart=/opt/wall/.venv/bin/python -m pcwall server",
        f"StandardOutput=append:{_posix(root)}/logs/pcwall.log",
    ]
    assert _missing(proc.stdout, unit) == []
    assert _missing(proc.stdout, ["systemctl daemon-reload", "systemctl enable pcwall.service"]) == []
    assert not (root / "logs").exists(), "a dry run must not touch the file system"


def test_install_service_sh_rejects_bad_input(tmp_path: Path) -> None:
    if not _bash_ready():
        pytest.skip("no working bash on PATH")
    script = _lf_copy(SH, tmp_path)
    bogus = _run([BASH, _posix(script), "--dry-run", "--bogus"])
    assert bogus.returncode == 2 and "usage:" in bogus.stderr.lower()
    missing = _run([BASH, _posix(script), "--dry-run", "--root", _posix(tmp_path / "nowhere")])
    assert missing.returncode == 1 and "nowhere" in missing.stderr
    remove = _run([BASH, _posix(script), "--dry-run", "--uninstall", "--root", _posix(tmp_path)])
    assert remove.returncode == 0 and "systemctl disable --now pcwall.service" in remove.stdout


# --- scripts/install_service.ps1 ---------------------------------------------------------


def test_install_service_ps1_names_the_service_and_logs() -> None:
    text = PS1.read_text(encoding="utf-8")
    assert _missing(text, ["[switch]$DryRun", "ServiceName = 'pcwall'", "AppStdout", "AppStderr", "AppExit"]) == []


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
def test_install_service_ps1_dry_run_lists_nssm_commands(tmp_path: Path) -> None:
    root = tmp_path / "wall"
    root.mkdir()
    (root / ".env").write_text("# comment\nPCWALL_PORT=8770\nPCWALL_KEY=do-not-print-me\n", encoding="utf-8")
    python = r"C:\wall\.venv\Scripts\python.exe"
    proc = _run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(PS1),
         "-DryRun", "-Root", str(root), "-Python", python, "-Nssm", r"C:\tools\nssm.exe"]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    commands = [
        f'nssm install pcwall "{python}"', f'nssm set pcwall AppDirectory "{root}"',
        f'nssm set pcwall AppStdout "{root / "logs" / "pcwall.log"}"',
        "nssm set pcwall AppExit Default Restart", "PCWALL_KEY=<set>", "nssm start pcwall",
    ]
    assert _missing(proc.stdout, commands) == []
    assert "do-not-print-me" not in proc.stdout, "values from .env must never be printed"
    assert not (root / "logs").exists(), "a dry run must not touch the file system"


# --- README.md and the sanitization gate -------------------------------------------------


def test_readme_covers_the_published_surface() -> None:
    text = README.read_text(encoding="utf-8")
    headings = " | ".join(line.lstrip("#").strip().lower() for line in text.splitlines() if line.startswith("#"))
    assert _missing(headings, ["quickstart", "configuration", "google", "ai agent", "scaling", "service"]) == []
    assert _missing(text, ["Path A", "Path B", "python -m pcwall.google_auth", "?preset=tablet_15_6",
                           "install_service.ps1", "install_service.sh"]) == []
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert _blockquote(text, "> You are customising") == _blockquote(agents, "> You are customising")
    links = re.findall(r"\]\((?!https?://|#|mailto:)([^)#\s]+)", text)
    assert [target for target in links if not (ROOT / target).exists()] == []


def test_published_files_pass_sanitization() -> None:
    for path in (README, PS1, SH, Path(__file__)):
        rel = path.relative_to(ROOT).as_posix()
        assert sc.scan_file(path, rel=rel) == [], rel

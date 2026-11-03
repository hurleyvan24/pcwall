#!/usr/bin/env bash
# Install, update or remove the pcwall systemd service (pcwall.service).
#
# Registers "python -m pcwall server" as a system service that starts at boot,
# restarts whenever it exits and appends its console output to logs/ under the
# repository. Run it from the checkout on the machine that will host the wall:
#
#     sudo scripts/install_service.sh                # install or update, then (re)start
#     sudo scripts/install_service.sh --uninstall    # stop, disable and remove the unit
#     scripts/install_service.sh --dry-run           # print the unit and commands only
#
# Options:
#   --root DIR      repository root (default: the parent of this script's directory)
#   --user NAME     account the service runs as (default: the user who ran sudo)
#   --python PATH   interpreter (default: <root>/.venv/bin/python, else python3 on PATH)
#   --dry-run       print the unit file and every command without touching the system
#   --uninstall     remove the service instead of installing it
#   -h, --help      show this text
#
# The service reads its environment from <root>/.env through EnvironmentFile=,
# so PCWALL_PORT, PCWALL_KEY and the rest live there, never in this script.
# Requires systemd 240 or newer for the append: log targets.

set -euo pipefail

SERVICE_NAME="pcwall"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

ROOT=""
SERVICE_USER=""
PYTHON=""
DRY_RUN=0
UNINSTALL=0

usage() {
    cat <<'TEXT'
Usage: install_service.sh [--root DIR] [--user NAME] [--python PATH] [--dry-run] [--uninstall]
  --root DIR      repository root (default: the parent of this script's directory)
  --user NAME     account the service runs as (default: the user who ran sudo)
  --python PATH   interpreter (default: <root>/.venv/bin/python, else python3 on PATH)
  --dry-run       print the unit file and every command without touching the system
  --uninstall     remove the service instead of installing it
  -h, --help      show this text
TEXT
}

die() {
    printf 'install_service.sh: %s\n' "$*" >&2
    exit 1
}

# Echo a command, then run it unless this is a dry run.
run() {
    printf '+ %s\n' "$*"
    if [[ "$DRY_RUN" -eq 0 ]]; then
        "$@"
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)      [[ $# -ge 2 ]] || { usage >&2; exit 2; }; ROOT="$2"; shift 2 ;;
        --user)      [[ $# -ge 2 ]] || { usage >&2; exit 2; }; SERVICE_USER="$2"; shift 2 ;;
        --python)    [[ $# -ge 2 ]] || { usage >&2; exit 2; }; PYTHON="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           printf 'install_service.sh: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$ROOT" ]]; then
    ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fi
[[ -d "$ROOT" ]] || die "repository root does not exist: $ROOT"
ROOT="$(cd "$ROOT" && pwd -P)"

if [[ "$DRY_RUN" -eq 0 && "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "run with sudo (or add --dry-run to preview without changing anything)"
fi

if [[ "$UNINSTALL" -eq 1 ]]; then
    run systemctl disable --now "${SERVICE_NAME}.service" || true
    run rm -f "$UNIT_PATH"
    run systemctl daemon-reload
    printf '%s removed.\n' "${SERVICE_NAME}.service"
    exit 0
fi

if [[ -z "$SERVICE_USER" ]]; then
    SERVICE_USER="${SUDO_USER:-$(id -un)}"
fi
if [[ "$DRY_RUN" -eq 0 ]]; then
    id -u "$SERVICE_USER" >/dev/null 2>&1 || die "no such user: $SERVICE_USER (create it, or pass --user)"
fi
SERVICE_GROUP="$(id -gn "$SERVICE_USER" 2>/dev/null || printf '%s' "$SERVICE_USER")"

if [[ -z "$PYTHON" ]]; then
    if [[ -x "$ROOT/.venv/bin/python" ]]; then
        PYTHON="$ROOT/.venv/bin/python"
    else
        PYTHON="$(command -v python3 2>/dev/null || printf '/usr/bin/python3')"
    fi
fi
if [[ "$DRY_RUN" -eq 0 && ! -x "$PYTHON" ]]; then
    die "python interpreter is not executable: $PYTHON (pass --python)"
fi

render_unit() {
    cat <<UNIT
[Unit]
Description=pcwall family wall display
Documentation=file://${ROOT}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${ROOT}
EnvironmentFile=-${ROOT}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYTHON} -m pcwall server
Restart=always
RestartSec=5
StandardOutput=append:${ROOT}/logs/pcwall.log
StandardError=append:${ROOT}/logs/pcwall.err.log
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
}

run mkdir -p "$ROOT/logs" "$ROOT/data"
run chown "${SERVICE_USER}:${SERVICE_GROUP}" "$ROOT/logs" "$ROOT/data"

printf '+ write %s\n' "$UNIT_PATH"
if [[ "$DRY_RUN" -eq 1 ]]; then
    printf -- '----- %s -----\n' "$UNIT_PATH"
    render_unit
    printf -- '----- end of unit -----\n'
else
    render_unit > "$UNIT_PATH"
    chmod 644 "$UNIT_PATH"
fi

run systemctl daemon-reload
run systemctl enable "${SERVICE_NAME}.service"
run systemctl restart "${SERVICE_NAME}.service"

if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'dry run: nothing was written or started.\n'
else
    printf '%s is enabled and running as %s from %s.\n' "${SERVICE_NAME}.service" "$SERVICE_USER" "$ROOT"
    printf 'Logs: %s/logs/pcwall.log and pcwall.err.log. Status: systemctl status %s\n' "$ROOT" "$SERVICE_NAME"
fi

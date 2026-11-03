"""CLI and service entrypoint for Family Wall.

Running with no arguments (or 'server') starts the Starlette/uvicorn service.
Running with 'pair' or 'status' runs CLI administration.

Usage:
    python -m pcwall [server]
    python -m pcwall pair [kiosk|parent] [member]
    python -m pcwall status
"""

import os
import sys


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "server"

    if cmd == "server":
        import uvicorn
        from pcwall.server import app, DEFAULT_PORT
        host = os.environ.get("PCWALL_HOST", "0.0.0.0")
        port = int(os.environ.get("PCWALL_PORT", DEFAULT_PORT))
        print(f"Starting Family Wall on {host}:{port}...")
        uvicorn.run(app, host=host, port=port, log_level="info", timeout_graceful_shutdown=5)

    elif cmd == "pair":
        from tools.pcwall_client import WallProblem, call
        role = args[1] if len(args) > 1 else "kiosk"
        member = args[2] if len(args) > 2 else ("alex" if role == "parent" else None)
        try:
            res = call("pairing_code", {"role": role, "member": member, "seconds": 86400}, actor="alex")
            print(f"\n==========================================")
            print(f" FAMILY WALL PAIRING CODE: {res['code']}")
            print(f" Role: {res['role']} (Valid for {res['minutes']} minutes)")
            print(f"==========================================")
            print(f"Enter this 6-digit code on your tablet/screen keypad to connect!\n")
        except WallProblem as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif cmd == "status":
        from tools.pcwall_client import WallProblem, call
        try:
            res = call("status", actor="alex")
            print(f"Family Wall Status (rev {res['revision']}):")
            print(f"  Devices: {len(res['devices'])} paired")
            print(f"  Calendar: {res['calendar']['events']} events")
            print(f"  PIN set: {res['pin_set']}")
        except WallProblem as e:
            print(f"Error: {e}")
            sys.exit(1)

    else:
        print("Usage: python -m pcwall [server|pair|status]")


if __name__ == "__main__":
    main()

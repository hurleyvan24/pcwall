"""Who may do what: HomeAgent's key, paired devices, pairing codes and the parent PIN.

Roles: "kiosk" (the kitchen panel: kids tick their own lists and ask for rewards),
"parent" (a paired phone: everything), "agent" (HomeAgent with PCWALL_KEY,
acting for the parent named in X-Wall-Actor). See docs/SKYLIGHT-CLONE-DESIGN.md
section 8.
"""

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass

from pcwall import engine

ROLES = ("kiosk", "parent")
CODE_SECONDS = 600              # a pairing code works for ten minutes, once
CODE_FAILURES = 5               # wrong codes one address may try per CODE_FAILURE_SECONDS
CODE_FAILURE_SECONDS = 60
CODE_FAILURES_TOTAL = 10        # wrong codes from everyone per CODE_SECONDS: the backstop against many addresses
PIN_ROUNDS = 200_000
# What an agent's X-Wall-Actor may put in the ledger when it names no parent: short, printable, plain.
ACTOR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.@-]{0,39}")


@dataclass(frozen=True)
class Principal:
    role: str                   # "kiosk", "parent" or "agent"
    member: str = None          # the parent behind a phone or an agent call
    device: str = None
    device_name: str = None     # the paired device's label, for intercom and SOS tags
    actor_override: str = None  # an agent's X-Wall-Actor when it names no parent, still recorded as "by"

    @property
    def actor(self) -> str:
        """What the ledger records as "by"."""
        return self.actor_override or self.member or ("wall" if self.role == "kiosk" else self.role)


@dataclass(frozen=True)
class Refusal:
    """What from_key answers instead of a Principal when a key is offered but cannot be honoured.
    It is never a caller: every handler turns it into the HTTP error it names."""
    error: str
    status: int = 403


AGENT_CALLS_DISABLED = Refusal("agent_calls_disabled")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parent_ids(state: dict) -> set:
    return {m["id"] for m in state["members"] if m["role"] == "parent"}


def member_ids(state: dict) -> set:
    """Every kid and parent a device or an intercom message can be tagged with."""
    return {m["id"] for m in state["members"] if m.get("id") and m.get("id") != "family"}


def from_key(key: str, wall_key: str, actor: str, state: dict):
    """The agent Principal for the wall's key; None for a wrong one (the caller is then unpaired);
    AGENT_CALLS_DISABLED when a key is offered but the wall has none. The comparison is
    constant-time and never made against an empty key."""
    if not key:
        return None
    if not wall_key:
        return AGENT_CALLS_DISABLED
    if not hmac.compare_digest(key.encode("utf-8"), wall_key.encode("utf-8")):
        return None
    actor = (actor or "").strip()
    if actor in parent_ids(state):
        return Principal("agent", actor)
    return Principal("agent", None, actor_override=actor if ACTOR_PATTERN.fullmatch(actor) else None)


def from_cookie(token: str, state: dict):
    if not token:
        return None
    digest = token_hash(token)
    for device in state.get("devices") or ():
        if not device.get("revoked") and hmac.compare_digest(device["hash"], digest):
            return Principal(device["role"], device.get("member"), device["id"], device.get("name"))
    return None


class Limiter:
    """At most `limit` failures in `window` seconds, then refuse until they age out."""

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.failures = []

    def count(self) -> int:
        """Failures still inside the window; older ones are forgotten here."""
        now = self.clock()
        self.failures = [t for t in self.failures if now - t < self.window]
        return len(self.failures)

    def blocked(self) -> bool:
        return self.count() >= self.limit

    def fail(self) -> None:
        self.failures.append(self.clock())


class Pairing:
    """Six-digit, single-use codes held in memory only: a restart voids every code.

    Wrong guesses are limited per address (CODE_FAILURES in CODE_FAILURE_SECONDS) and, as a
    backstop against guessing from many addresses, in total (CODE_FAILURES_TOTAL in CODE_SECONDS).
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.codes = {}
        self.limiter = Limiter(CODE_FAILURES_TOTAL, CODE_SECONDS, clock)
        self.limiters = {}          # address -> Limiter, kept only while it holds a recent failure
        self._lock = threading.RLock()

    def issue(self, role: str, member=None, seconds: int = None) -> dict:
        if role not in ROLES:
            raise engine.WallError("A device is paired as 'kiosk' or 'parent'.")
        duration = CODE_SECONDS if seconds is None else int(seconds)
        with self._lock:
            self._expire()
            code = f"{secrets.randbelow(10 ** 6):06d}"
            while code in self.codes:
                code = f"{secrets.randbelow(10 ** 6):06d}"
            self.codes[code] = {"role": role, "member": member, "expires": self.clock() + duration}
            return {"code": code, "role": role, "member": member, "minutes": duration // 60}

    def redeem(self, code: str, ip: str = None):
        """The grant for a good code, None for a wrong one; PermissionError once `ip` (or everyone
        together) has guessed wrong too often. A refused try neither spends nor expires a code."""
        address = str(ip or "unknown")
        with self._lock:
            limiter = self._limiter(address)
            if limiter.blocked():
                raise PermissionError("Too many wrong codes. Wait one minute and try again.")
            if self.limiter.blocked():
                raise PermissionError("Too many wrong codes. Wait ten minutes and ask for a new one.")
            self._expire()
            grant = self.codes.pop(str(code or "").strip(), None)
            if grant is None:
                limiter.fail()
                self.limiter.fail()
            return grant

    def _limiter(self, address: str) -> Limiter:
        """This address's limiter, after dropping every other address whose failures have aged out,
        so the table never grows past the addresses that failed in the last minute."""
        self.limiters = {ip: limiter for ip, limiter in self.limiters.items()
                         if ip == address or limiter.count()}
        limiter = self.limiters.get(address)
        if limiter is None:
            limiter = self.limiters[address] = Limiter(CODE_FAILURES, CODE_FAILURE_SECONDS, self.clock)
        return limiter

    def _expire(self) -> None:
        now = self.clock()
        self.codes = {code: grant for code, grant in self.codes.items() if grant["expires"] > now}


def add_device(state: dict, grant: dict, name: str, now):
    """Store only the token's hash; the token itself goes to the device as a cookie."""
    token = secrets.token_urlsafe(32)
    device = {"id": engine.new_id("dv"), "name": (name or grant["role"]).strip()[:40] or grant["role"],
              "role": grant["role"], "member": grant.get("member"), "hash": token_hash(token),
              "created_at": now.isoformat()}
    state.setdefault("devices", []).append(device)
    return token, {key: value for key, value in device.items() if key != "hash"}


def revoke_device(state: dict, device_id: str) -> dict:
    for device in state.get("devices") or ():
        if device["id"] == device_id and not device.get("revoked"):
            device["revoked"] = True
            return {"id": device["id"], "name": device["name"]}
    raise engine.WallError("No paired device has that id.")


_UNSET = object()


def rename_device(state: dict, device_id: str, *, name=None, member=_UNSET) -> dict:
    """Relabel a paired device and, when `member` is passed, set or clear whose it is."""
    for device in state.get("devices") or ():
        if device["id"] == device_id and not device.get("revoked"):
            break
    else:
        raise engine.WallError("No paired device has that id.")
    if name is not None:
        if not (isinstance(name, str) and name.strip()):
            raise engine.WallError("Give the device a name.")
        device["name"] = name.strip()[:40]
    if member is not _UNSET:
        if member is None or member == "":
            device["member"] = None
        elif isinstance(member, str) and member in member_ids(state):
            device["member"] = member
        else:
            raise engine.WallError("Unknown family member.")
    return {key: value for key, value in device.items() if key != "hash"}


def hash_pin(pin: str, salt: str = None) -> str:
    if not (isinstance(pin, str) and pin.isascii() and pin.isdigit() and 4 <= len(pin) <= 8):
        raise engine.WallError("The parent PIN is 4 to 8 digits.")
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), PIN_ROUNDS).hex()
    return f"pbkdf2_sha256${PIN_ROUNDS}${salt}${digest}"


def verify_pin(pin, stored) -> bool:
    """Check a PIN without accepting malformed or unsupported stored hashes."""
    if not isinstance(stored, str) or not isinstance(pin, str):
        return False
    if not (pin.isascii() and pin.isdigit() and 4 <= len(pin) <= 8):
        return False
    parts = stored.split("$")
    if len(parts) != 4:
        return False
    algorithm, rounds, salt, digest = parts
    if (algorithm != "pbkdf2_sha256" or rounds != str(PIN_ROUNDS) or not salt
            or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
        return False
    attempt = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), PIN_ROUNDS).hex()
    return hmac.compare_digest(attempt, digest)


check_pin = verify_pin

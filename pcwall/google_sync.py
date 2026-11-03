"""Read-only Google connectors using this wall's local OAuth token.

Calendar colours come from configured member IDs. Photos Picker URLs are
temporary, so screen-sized copies are kept in
data/pcwall_photos/ for offline playback. Google clients are created lazily;
importing this module never reads credentials or contacts Google.
"""

import asyncio
import base64
import hashlib
import html as html_text
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pcwall import google_auth, model
from pcwall.engine import WallError

PICKER = "https://photospicker.googleapis.com/v1"
EVENT_FIELDS = "items(id,summary,location,start,end,status),nextPageToken"
PHOTO_SIZE = (1920, 1080)
PICK_AT_MOST = 200
DEFAULT_PHOTO_DIR = Path(__file__).resolve().parent.parent / "data" / "pcwall_photos"
# Scan recent school notices; households can restrict the query to their own senders.
DRESS_DAY_QUERY = os.environ.get("PCWALL_DRESS_QUERY", "subject:dress OR subject:spirit")
DRESS_DAY_MAX_RESULTS = 15
DRESS_DAY_PATTERN = re.compile(
    r"\b(?:free\s+dress|dress(?:ed|es|ing|-up)?|wear(?:ing|s)?|spirit|pajamas?|pyjamas?|dot\s+day|dots|"
    r"crazy\s+hair|red\s+ribbon|twin\s+day|theme\s+day|costumes?)\b", re.IGNORECASE)
DRESS_DAY_SKIP = re.compile(r"weekly\s+progress\s+for", re.IGNORECASE)   # the routine report, never a dress day
MONTHS = {name: number for number, names in enumerate(
    (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
     ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
     ("dec", "december")), start=1) for name in names}
MONTH_DAY = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
                       re.IGNORECASE)
NUMERIC_DAY = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b")
logger = logging.getLogger(__name__)


def tag_members(title: str, members: list, default=None) -> list:
    """Names/aliases and group words override the calendar's default member."""
    text = model.normalize(title)
    tagged = set()
    for member in members:
        if member["role"] == "shared":
            continue
        for name in (member["name"], *member.get("aliases", [])):
            name = model.normalize(name)
            if name and re.search(rf"\b{re.escape(name)}\b", text):
                tagged.add(member["id"])
    if re.search(r"\btwins\b", text):
        tagged.update(m["id"] for m in members if m.get("twin"))
    if re.search(r"\b(kids|children)\b", text):
        tagged.update(m["id"] for m in members if m["role"] == "kid")
    return [m["id"] for m in members if m["id"] in tagged] or [default or "family"]


def normalize_event(raw: dict, calendar: dict, members: list, zone) -> dict:
    start, end = raw.get("start", {}), raw.get("end", {})
    all_day = "date" in start

    def local_time(value):
        instant = datetime.fromisoformat(value["dateTime"])
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=ZoneInfo(value["timeZone"]) if value.get("timeZone") else zone)
        return instant.astimezone(zone).isoformat()

    if all_day:
        begin, finish = start["date"], end.get("date", start["date"])
    else:
        begin, finish = local_time(start), local_time(end)
    title = (raw.get("summary") or "Busy").strip() or "Busy"
    return {"id": raw["id"], "calendar": calendar["id"], "title": title,
            "location": raw.get("location") or "", "all_day": all_day,
            "start": begin, "end": finish,
            "members": tag_members(title, members, calendar.get("member"))}


def fetch_events(service, calendars: list, members: list, *, start, end, zone) -> list:
    events = []
    for calendar in calendars:
        page = None
        while True:
            query = {"calendarId": calendar["id"], "timeMin": start.isoformat(), "timeMax": end.isoformat(),
                     "singleEvents": True, "orderBy": "startTime", "maxResults": 250, "fields": EVENT_FIELDS}
            if page:
                query["pageToken"] = page
            response = service.events().list(**query).execute()
            events.extend(normalize_event(raw, calendar, members, zone)
                          for raw in response.get("items", []) if raw.get("status") != "cancelled")
            page = response.get("nextPageToken")
            if not page:
                break
    return sorted(events, key=lambda event: (event["start"], event["title"], event["calendar"], event["id"]))


def list_calendars(service) -> list:
    found, page = [], None
    while True:
        query = {"minAccessRole": "reader"}
        if page:
            query["pageToken"] = page
        response = service.calendarList().list(**query).execute()
        found.extend({"id": c["id"], "name": c.get("summaryOverride") or c.get("summary", c["id"]),
                      "primary": bool(c.get("primary"))} for c in response.get("items", []))
        page = response.get("nextPageToken")
        if not page:
            return found


def seconds(duration, default: float) -> float:
    """Parse Google's duration strings, e.g. '3.5s'."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(duration or ""))
    return float(match.group(1)) if match else default


class PhotoPicker:
    """Picker REST calls over an authorized requests session (or an offline fake)."""

    def __init__(self, http, photo_dir=DEFAULT_PHOTO_DIR, size=PHOTO_SIZE):
        self.http, self.photo_dir, self.size = http, Path(photo_dir), size

    @staticmethod
    def _json(response) -> dict:
        response.raise_for_status()
        return response.json() if response.content else {}

    def create(self, max_items: int = PICK_AT_MOST) -> dict:
        return self._json(self.http.post(f"{PICKER}/sessions", timeout=30,
                                         json={"pickingConfig": {"maxItemCount": str(max_items)}}))

    def get(self, session_id: str) -> dict:
        return self._json(self.http.get(f"{PICKER}/sessions/{session_id}", timeout=30))

    def delete(self, session_id: str) -> None:
        self.http.delete(f"{PICKER}/sessions/{session_id}", timeout=30).raise_for_status()

    def items(self, session_id: str) -> list:
        found, page = [], None
        while True:
            params = {"sessionId": session_id, "pageSize": 100}
            if page:
                params["pageToken"] = page
            body = self._json(self.http.get(f"{PICKER}/mediaItems", params=params, timeout=30))
            found.extend(body.get("mediaItems", []))
            page = body.get("nextPageToken")
            if not page:
                return found

    def download(self, item: dict):
        """Atomically cache photos; skip videos and reuse previously picked files."""
        if item.get("type") != "PHOTO":
            return None
        media = item["mediaFile"]
        name = hashlib.sha1(item["id"].encode("utf-8")).hexdigest()[:16] + ".jpg"
        path = self.photo_dir / name
        if not path.is_file():
            width, height = self.size
            response = self.http.get(f"{media['baseUrl']}=w{width}-h{height}", timeout=60)
            response.raise_for_status()
            if not response.content:
                raise ValueError("Google returned an empty photo.")
            self.photo_dir.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=name + ".", suffix=".part", dir=self.photo_dir)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(response.content)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        size = media.get("mediaFileMetadata", {})
        return {"file": name, "id": item["id"], "width": size.get("width"), "height": size.get("height"),
                "created": item.get("createTime")}

    def import_session(self, session_id: str) -> list:
        # Keep a failed session available for the next polling attempt.
        saved = [photo for photo in map(self.download, self.items(session_id)) if photo]
        try:
            self.delete(session_id)
        except Exception:
            logger.warning("Could not delete a completed photo picker session; it will expire.")
        return saved


def _remove_photo(photo_dir, name):
    root = Path(photo_dir).resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise WallError("Photo filenames must stay inside the photo cache.")
    path.unlink(missing_ok=True)


def merge_photos(state: dict, saved: list, now, photo_dir=DEFAULT_PHOTO_DIR) -> dict:
    """Newest picks first, deduplicated, with old files removed past the cap."""
    new = {photo["file"]: {**photo, "added_at": now.isoformat()} for photo in saved}
    items = list(new.values()) + [p for p in state["photos"]["items"] if p["file"] not in new]
    keep = max(0, state["settings"]["photos"]["max_items"])
    for dropped in items[keep:]:
        _remove_photo(photo_dir, dropped["file"])
    state["photos"].update(items=items[:keep], session=None, synced_at=now.isoformat(), error=None)
    return {"added": len(new), "total": len(state["photos"]["items"])}


def _authorized_session():
    credentials = _require_google(google_auth.get_credentials())
    from google.auth.transport.requests import AuthorizedSession
    return AuthorizedSession(credentials)


def _calendar_service():
    credentials = google_auth.get_credentials()
    if credentials is None:
        return None
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _gmail_service():
    credentials = _require_google(google_auth.get_credentials())
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


@dataclass
class GoogleHub:
    """Client factories; the wall and tests can inject their own clients."""
    calendar: object = _calendar_service
    http: object = _authorized_session
    gmail: object = _gmail_service


def _require_google(google):
    if google is None:
        raise WallError("Google isn't connected on this wall.")
    return google


def _record_error(store, section, exc):
    message = f"{type(exc).__name__}: {str(exc)[:200]}"
    store.transact(lambda draft, now: draft[section].update(error=message), kinds=(section,))
    return message


def sync_calendar_events(store, google: GoogleHub) -> dict:
    """Refresh selected calendars without losing the last good snapshot on error."""
    state = store.current()
    window = state["settings"]["calendar"]
    calendars = window["calendars"]
    if not calendars:
        return {"events": 0, "skipped": "No calendars chosen yet."}
    now = store.now().astimezone(store.zone)
    start = (now - timedelta(days=window["past_days"])).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=window["future_days"])).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        service = google.calendar() if google is not None else None
        if service is None:
            logger.info("Google Calendar is not connected; keeping cached events.")
            return {"events": len(state["calendar"]["events"]), "skipped": "Google isn't connected on this wall."}
        events = fetch_events(service, calendars, state["members"], start=start, end=end, zone=store.zone)
    except Exception as exc:
        message = _record_error(store, "calendar", exc)
        raise WallError(f"The calendar didn't sync ({message}).") from exc

    def save(draft, now):
        calendar = draft["calendar"]
        stale = calendar["synced_at"] is None or \
            now - datetime.fromisoformat(calendar["synced_at"]) >= timedelta(hours=1)
        if calendar["events"] != events or calendar["error"] or stale:
            calendar.update(events=events, error=None, synced_at=now.isoformat())
        return {"events": len(events)}

    return store.transact(save, kinds=("calendar",))


def start_picking(store, google: GoogleHub, photo_dir=DEFAULT_PHOTO_DIR) -> dict:
    try:
        session = PhotoPicker(_require_google(google).http(), photo_dir).create()
        now = store.now()
        config = session.get("pollingConfig", {})
        record = {"id": session["id"], "picker_uri": session["pickerUri"], "started_at": now.isoformat(),
                  "poll_seconds": max(1.0, seconds(config.get("pollInterval"), 5.0)),
                  "expires_at": (now + timedelta(seconds=seconds(config.get("timeoutIn"), 1800.0))).isoformat()}
    except Exception as exc:
        message = _record_error(store, "photos", exc)
        raise WallError(f"The photo picker didn't start ({message}).") from exc
    store.transact(lambda draft, now: draft["photos"].update(session=record, error=None), kinds=("photos",))
    return record


def sync_picked_photos(store, google: GoogleHub, photo_dir=DEFAULT_PHOTO_DIR) -> dict:
    """Poll a pending picker session, importing copies when selection is complete."""
    session = store.current()["photos"]["session"]
    if not session or google is None:
        return {"status": "idle"}

    def is_current(state):
        return (state["photos"]["session"] or {}).get("id") == session["id"]

    def pending(state):
        current = state["photos"]["session"]
        return {"status": "waiting", "poll_seconds": current["poll_seconds"]} if current else {"status": "idle"}

    def expire():
        def save(draft, now):
            if not is_current(draft):
                return pending(draft)
            draft["photos"].update(session=None, error="The photo picker timed out before anything was picked.")
            return {"status": "expired"}

        return store.transact(save, kinds=("photos",))

    try:
        picker = PhotoPicker(google.http(), photo_dir)
        try:
            selection = picker.get(session["id"])
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) in (404, 410):
                return expire()
            raise
        if selection.get("mediaItemsSet"):
            saved = picker.import_session(session["id"])

            def save(draft, now):
                if not is_current(draft):
                    return pending(draft)
                return {"status": "imported", **merge_photos(draft, saved, now, photo_dir)}

            return store.transact(save, kinds=("photos",))
        if store.now() >= datetime.fromisoformat(session["expires_at"]):
            return expire()
    except Exception as exc:
        message = f"{type(exc).__name__}: {str(exc)[:200]}"

        def save_error(draft, now):
            if is_current(draft):
                draft["photos"].update(error=message)

        store.transact(save_error, kinds=("photos",))
        raise WallError(f"The photos didn't sync ({message}).") from exc
    return pending(store.current())


# Preserve the plan's entry points alongside the task packet's public names.
sync_calendar = sync_calendar_events
check_picking = sync_picked_photos


# ── Elementary School dress days ──────────────────────────────────────────

def _school_year_date(month: int, day: int, received: date, year=None):
    """The date a school notice means by 'M/D' or 'Month D': the school year runs August to July."""
    if year is None:
        start = received.year if received.month >= 8 else received.year - 1
        year = start if month >= 8 else start + 1
    elif year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _dress_day_date(text: str, received: date):
    """'tomorrow' counts from the day the mail arrived; an explicit day lands in the current school
    year. None when the text names no day at all."""
    if re.search(r"\btomorrow\b", text, re.IGNORECASE):
        return received + timedelta(days=1)
    numeric, named = NUMERIC_DAY.search(text), MONTH_DAY.search(text)
    if numeric and (not named or numeric.start() < named.start()):
        return _school_year_date(int(numeric.group(1)), int(numeric.group(2)), received,
                                 int(numeric.group(3)) if numeric.group(3) else None)
    if named:
        return _school_year_date(MONTHS[named.group(1).lower()], int(named.group(2)), received)
    return None


def _clean_subject(subject) -> str:
    text = re.sub(r"^\s*(?:(?:re|fw|fwd)\s*:\s*|\[[^\]]*\]\s*)+", "", str(subject or ""), flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", html_text.unescape(text)).strip()
    return text[:120] or "School dress day"


def _clean_snippet(snippet) -> str:
    return re.sub(r"\s+", " ", html_text.unescape(str(snippet or ""))).strip()[:240]


def extract_dress_day_candidates(messages: list, today: date) -> list:
    """Dress, spirit and theme day notices from school mail, each with the day it is about.

    Pure: ``messages`` are dicts with ``id``, ``subject``, ``snippet``, ``body`` and
    ``received_date`` (YYYY-MM-DD). The subject and snippet name the day when they can, the body
    otherwise, and a notice with no day is about the day it arrived. Routine 'Weekly Progress
    for ...' reports are skipped; only days from ``today`` on come back, each 'pending' until a
    parent approves it, sorted by day.
    """
    found, seen = [], set()
    for message in messages:
        if not isinstance(message, dict) or not message.get("id"):
            continue
        subject, snippet, body = (str(message.get(key) or "") for key in ("subject", "snippet", "body"))
        if DRESS_DAY_SKIP.search(subject) or not DRESS_DAY_PATTERN.search(f"{subject} {snippet} {body}"):
            continue
        try:
            received = date.fromisoformat(str(message.get("received_date") or ""))
        except ValueError:
            received = today
        target = (_dress_day_date(f"{subject} {snippet}", received) or _dress_day_date(body, received)
                  or received).isoformat()
        message_id = str(message["id"])
        item_id = f"dd_{message_id[:10]}_{target}"
        if target < today.isoformat() or item_id in seen:
            continue
        seen.add(item_id)
        found.append({"id": item_id, "message_id": message_id, "date": target, "title": _clean_subject(subject),
                      "summary": _clean_snippet(snippet or body), "status": "pending"})
    return sorted(found, key=lambda item: (item["date"], item["id"]))


def _gmail_text(payload: dict) -> str:
    """Plain text from a Gmail payload tree; HTML-only mail has its tags stripped."""
    plain, rich, stack = [], [], [payload]
    while stack:
        part = stack.pop()
        if not isinstance(part, dict):
            continue
        stack.extend(reversed(part.get("parts") or []))
        data = str((part.get("body") or {}).get("data") or "")
        if not data:
            continue
        try:
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        except (ValueError, TypeError):
            continue
        (rich if "html" in str(part.get("mimeType") or "").lower() else plain).append(text)
    if plain:
        return "\n".join(plain)
    return html_text.unescape(re.sub(r"<[^>]+>", " ", "\n".join(rich)))


def _gmail_message(service, message_id: str, zone) -> dict:
    """One message as the scanner reads it: subject, snippet, text and the local day it arrived."""
    raw = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    headers = {str(h.get("name") or "").lower(): str(h.get("value") or "")
               for h in payload.get("headers") or [] if isinstance(h, dict)}
    try:
        received = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, tz=zone).date().isoformat()
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        received = ""
    return {"id": str(raw.get("id") or message_id), "subject": headers.get("subject", ""),
            "snippet": str(raw.get("snippet") or ""), "body": _gmail_text(payload), "received_date": received}


def sync_heritage_dress_days(store, google: GoogleHub) -> dict:
    """Scan Elementary School mail for dress and spirit days; new ones wait as 'pending' for a parent.

    A message is saved once (by id and message id), so a day a parent dismissed does not come
    back. The change is published under 'notes', which is where the wall draws the banner.
    """
    service = _require_google(google).gmail()
    today = store.now().astimezone(store.zone).date()
    listing = service.users().messages().list(userId="me", q=os.environ.get("PCWALL_DRESS_QUERY", DRESS_DAY_QUERY),
                                              maxResults=DRESS_DAY_MAX_RESULTS).execute()
    messages = [_gmail_message(service, ref["id"], store.zone)
                for ref in listing.get("messages") or [] if isinstance(ref, dict) and ref.get("id")]
    candidates = extract_dress_day_candidates(messages, today)
    if not candidates:
        return {"found": 0, "added": 0}

    def save(draft, now):
        items = draft.setdefault("dress_days", [])
        if not isinstance(items, list):
            items = draft["dress_days"] = []
        known_ids = {d.get("id") for d in items if isinstance(d, dict)}
        known_messages = {d.get("message_id") for d in items if isinstance(d, dict)}
        added = 0
        for candidate in candidates:
            if candidate["id"] in known_ids or candidate["message_id"] in known_messages:
                continue
            items.append(dict(candidate))
            known_ids.add(candidate["id"])
            known_messages.add(candidate["message_id"])
            added += 1
        return {"found": len(candidates), "added": added}

    return store.transact(save, kinds=("notes",))


async def calendar_loop(app) -> None:
    ctx = app.state.ctx
    while True:
        minutes = max(1, ctx.store.current()["settings"]["calendar"]["refresh_minutes"])
        if ctx.google is not None:
            try:
                await asyncio.to_thread(sync_calendar_events, ctx.store, ctx.google)
            except Exception as exc:
                logger.warning("Family Wall calendar sync failed: %s", exc)
            try:   # Its own guard: a Gmail problem must never cost the wall its calendar.
                await asyncio.to_thread(sync_heritage_dress_days, ctx.store, ctx.google)
            except Exception as exc:
                logger.warning("Family Wall dress day scan failed: %s", exc)
        await asyncio.sleep(minutes * 60)


def sync_shared_album(store, photo_dir=DEFAULT_PHOTO_DIR, sample_size: int = 60) -> dict:
    """Fetch and randomize photos from a shared Google Photos album URL."""
    import random
    import re
    import requests
    album_url = (store.current().get("settings", {}).get("photos", {}) or {}).get("album_url")
    if not album_url:
        return {"status": "no_album"}
    response = requests.get(album_url, timeout=20)
    response.raise_for_status()
    urls = list(dict.fromkeys(re.findall(r"https://lh3\.googleusercontent\.com/pw/[A-Za-z0-9_-]{40,}", response.text)))
    if not urls:
        return {"status": "empty"}
    random.shuffle(urls)
    root = Path(photo_dir)
    root.mkdir(parents=True, exist_ok=True)
    saved = []
    for url in urls[:sample_size]:
        pid = url.split("/pw/", 1)[-1][:48]
        name = hashlib.sha1(pid.encode("utf-8")).hexdigest()[:16] + ".jpg"
        path = root / name
        if not path.is_file():
            img = requests.get(f"{url}=w1920-h1200", timeout=30)
            if img.status_code != 200 or not img.content:
                continue
            descriptor, temporary = tempfile.mkstemp(prefix=name + ".", suffix=".part", dir=root)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(img.content)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        saved.append({"file": name, "id": pid, "width": 1920, "height": 1200, "created": None})
    if not saved:
        return {"status": "empty"}
    random.shuffle(saved)
    return store.transact(lambda draft, now: merge_photos(draft, saved, now, root), kinds=("photos",))


async def photo_loop(app) -> None:
    ctx = app.state.ctx
    while True:
        delay = 5.0
        current = ctx.store.current()
        if ctx.google is not None and current["photos"]["session"]:
            try:
                result = await asyncio.to_thread(sync_picked_photos, ctx.store, ctx.google, ctx.photo_dir)
                delay = max(1.0, result.get("poll_seconds", delay))
            except Exception as exc:
                logger.warning("Family Wall photo sync failed: %s", exc)
                delay = 30.0
        elif (current.get("settings", {}).get("photos", {}) or {}).get("album_url"):
            synced_at = current["photos"].get("synced_at")
            stale = synced_at is None or (ctx.store.now() - datetime.fromisoformat(synced_at)) >= timedelta(hours=6)
            if stale or not current["photos"]["items"]:
                try:
                    await asyncio.to_thread(sync_shared_album, ctx.store, ctx.photo_dir)
                except Exception as exc:
                    logger.warning("Family Wall shared album sync failed: %s", exc)
            delay = 300.0
        await asyncio.sleep(delay)


def _calendars(store, args, caller, ctx):
    shown = {c["id"] for c in store.current()["settings"]["calendar"]["calendars"]}
    found = list_calendars(_require_google(ctx.google).calendar())
    return {"calendars": [{**c, "shown": c["id"] in shown} for c in found]}


def _set_calendars(state, args, caller, now, ctx):
    chosen = args.get("calendars")
    if not isinstance(chosen, list) or not all(
            isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"].strip() for c in chosen):
        raise WallError('Give calendars as [{"id": ..., "member": ...}].')
    members = [m["id"] for m in state["members"]]
    for calendar in chosen:
        if calendar.get("member") not in [None, *members]:
            raise WallError(f"No member '{calendar['member']}'.")
    state["settings"]["calendar"]["calendars"] = [{"id": c["id"], "member": c.get("member")} for c in chosen]
    return {"calendars": state["settings"]["calendar"]["calendars"]}


def _refresh_calendar(store, args, caller, ctx):
    return sync_calendar_events(store, _require_google(ctx.google))


def _pick_photos(store, args, caller, ctx):
    record = start_picking(store, _require_google(ctx.google), ctx.photo_dir)
    return {"picker_uri": record["picker_uri"], "expires_at": record["expires_at"]}


def _clear_photos(state, args, caller, now, ctx):
    for photo in state["photos"]["items"]:
        _remove_photo(ctx.photo_dir, photo["file"])
    removed = len(state["photos"]["items"])
    state["photos"].update(items=[], session=None, error=None)
    return {"removed": removed}


def _dress_day(state: dict, args: dict) -> dict:
    wanted = args.get("id")
    if not isinstance(wanted, str) or not wanted.strip():
        raise WallError("Missing 'id'.")
    for item in state.get("dress_days") or []:
        if isinstance(item, dict) and item.get("id") == wanted:
            return item
    raise WallError(f"No dress day '{wanted}'.")


def _approve_dress_day(state, args, caller, now, ctx):
    """Open to everyone on purpose: a parent taps Approve on the wall itself or on a phone."""
    item = _dress_day(state, args)
    if args.get("date") is not None:
        try:
            item["date"] = date.fromisoformat(str(args["date"])).isoformat()
        except ValueError:
            raise WallError("Give the date as YYYY-MM-DD.") from None
    item.update(status="approved", decided_by=caller.actor, decided_at=now.isoformat())
    return {"dress_day": dict(item)}


def _dismiss_dress_day(state, args, caller, now, ctx):
    item = _dress_day(state, args)
    item.update(status="dismissed", decided_by=caller.actor, decided_at=now.isoformat())
    return {"dress_day": dict(item)}


def register_ops():
    """Wire into Phase 2 when installed; connectors also work without the web app."""
    try:
        from pcwall.ops import EVERYONE, PARENTS, op
    except ModuleNotFoundError as exc:
        if exc.name != "pcwall.ops":
            raise
        return
    op("calendars", PARENTS, in_transaction=False)(_calendars)
    op("set_calendars", PARENTS, kinds=("settings",))(_set_calendars)
    op("refresh_calendar", PARENTS, in_transaction=False)(_refresh_calendar)
    op("pick_photos", PARENTS, in_transaction=False)(_pick_photos)
    op("clear_photos", PARENTS, kinds=("photos",))(_clear_photos)
    op("approve_dress_day", EVERYONE, kinds=("notes",))(_approve_dress_day)
    op("dismiss_dress_day", EVERYONE, kinds=("notes",))(_dismiss_dress_day)


register_ops()

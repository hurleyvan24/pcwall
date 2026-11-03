"""The FamilyWall web service: Starlette on uvicorn, port 8770 by default.

Routes: /healthz, /api/state, /api/events (server-sent events), /api/do, /api/pair,
/api/intercom/upload, /photos/{file}, /videos/{file}, /intercom/{file} and the static app.
The store is the single writer of
data/pcwall.json; HomeAgent reaches it over HTTP with PCWALL_KEY.
See docs/SKYLIGHT-CLONE-DESIGN.md section 2.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import socket
import threading
from datetime import timedelta, timezone
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from pcwall import access, engine, events, news, ops, views, weather
from pcwall.store import ROOT, Store

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PHOTO_DIR = os.path.join(ROOT, "data", "pcwall_photos")
VIDEO_DIR = os.path.join(ROOT, "data", "pcwall_videos")
INTERCOM_DIR = os.path.join(ROOT, "data", "pcwall_intercom")
COOKIE = "fw_device"
COOKIE_DAYS = 400               # Chrome caps a cookie's lifetime at 400 days
PHOTO_NAME = re.compile(r"^[0-9a-f]{16}\.(jpg|png|webp)$")
VIDEO_NAME = re.compile(r"^[0-9a-f]{16}\.(mp4|webm)$")
INTERCOM_NAME = re.compile(r"^[0-9a-f]{16}\.(webm|mp4|wav|ogg|m4a)$")
INTERCOM_MAX_BYTES = 512 * 1024  # one recorded intercom clip (the app's body limit is the larger VIDEO_REPLY_MAX_BYTES)
# The extension a clip is stored under, by the MIME type the tablet's recorder reports; anything else is WebM.
INTERCOM_EXTENSIONS = {"audio/mp4": ".mp4", "video/mp4": ".mp4", "audio/x-m4a": ".mp4",
                       "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/ogg": ".ogg"}
DEFAULT_PORT = 8770
logger = logging.getLogger(__name__)
API_JSON_MAX_BYTES = 64 * 1024
VIDEO_REPLY_MAX_BYTES = 16 * 1024 * 1024
INTERCOM_TTL_MINUTES = 15


# Python on Windows takes MIME types from the registry, where .js is sometimes
# text/plain; a module script served that way never runs. Pin what the app serves.
for _extension, _mime in ((".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css"),
                          (".webmanifest", "application/manifest+json"), (".svg", "image/svg+xml"),
                          (".mp4", "video/mp4"), (".webm", "video/webm"),
                          (".wav", "audio/wav"), (".ogg", "audio/ogg"), (".m4a", "audio/mp4")):
    mimetypes.add_type(_mime, _extension)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _refused(who):
    """The error for a caller that may not proceed: unpaired (401) or a key the wall cannot honour
    (403 with the refusal's name, 'agent_calls_disabled'); None when `who` is a Principal."""
    if who is None:
        return _error(401, "This screen isn't paired with the wall yet.")
    if isinstance(who, access.Refusal):
        return _error(who.status, who.error)
    return None


def _key_from_file() -> str:
    """data/pcwall_key.txt, read only when PCWALL_KEY is unset; blank when there is none."""
    try:
        with open(os.path.join(ROOT, "data", "pcwall_key.txt"), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _lan_ip() -> str:
    """The address this machine uses on the LAN: a UDP socket "connected" to a documentation
    address picks the outbound route and sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def startup_banner(pairing: access.Pairing, host: str = None, port=None, public_url: str = None) -> tuple:
    """Print and log where the wall is with a fresh kiosk pairing code and a parent one, so the first
    screen and a parent's phone can both be paired from the console before any agent exists.
    Returns (url, kiosk_code); the parent code is only shown. `host` and `port` default to
    PCWALL_HOST and PCWALL_PORT, a wildcard host becomes the LAN address, and `public_url` wins
    when given."""
    if public_url and public_url.strip():
        url = public_url.strip().rstrip("/") + "/"
    else:
        host = (host or os.environ.get("PCWALL_HOST") or "").strip()
        if host in ("", "0.0.0.0", "::", "*"):
            host = _lan_ip()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = int(port or os.environ.get("PCWALL_PORT") or DEFAULT_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        url = f"http://{host}:{port}/"
    kiosk = pairing.issue("kiosk")
    parent = pairing.issue("parent")
    lines = (f"pcwall is ready at {url}",
             f"Pair the kitchen screen with code {kiosk['code']} (kiosk, valid {kiosk['minutes']} minutes).",
             f"Pair a parent's phone with code {parent['code']} (parent, valid {parent['minutes']} minutes).")
    for line in lines:
        logger.info(line)
    print("\n".join(lines), flush=True)
    return url, kiosk["code"]


def seconds_to_midnight(now) -> float:
    """Measured in UTC so a daylight-saving night is still the right length."""
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return (midnight.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()


class _StaticFiles(StaticFiles):
    """Keep the root mount available before Phase 5 supplies the static directory."""

    async def check_config(self):
        if os.path.exists(self.directory):
            await super().check_config()


def create_app(store: Store = None, *, wall_key: str = None, bus: events.EventBus = None,
               pairing: access.Pairing = None, background: bool = True, static_dir: str = STATIC_DIR,
               photo_dir: str = PHOTO_DIR, tasks=(), google=None,
               video_dir: str = VIDEO_DIR, intercom_dir: str = INTERCOM_DIR,
               banner: bool = None) -> Starlette:
    """`tasks` are extra coroutine functions task(app) started with the app (Phase 3's syncs).
    `video_dir` holds the 24-hour video messages served at /videos/{name}; `intercom_dir` the
    fifteen-minute intercom clips served at /intercom/{name}.

    `wall_key` None reads PCWALL_KEY whenever it is set, blank included, and data/pcwall_key.txt
    only when it is not; blank (either way) disables agent calls: a request offering a key is then
    refused with 403 'agent_calls_disabled'. `banner` prints the LAN URL and the kiosk and parent
    pairing codes at startup; it follows `background` by default."""
    bus = bus or events.EventBus()
    if wall_key is None:
        # A PCWALL_KEY that is set but blank is the operator's word: the key file is never consulted.
        wall_key = os.environ["PCWALL_KEY"] if "PCWALL_KEY" in os.environ else _key_from_file()
    wall_key = (wall_key or "").strip()
    ctx = SimpleNamespace(store=store, bus=bus, pairing=pairing or access.Pairing(),
                          pin_limiter=access.Limiter(5, 600), photo_dir=photo_dir, video_dir=video_dir,
                          intercom_dir=intercom_dir, google=google, tasks=tuple(tasks),
                          loop=None, logger=logger, banner=None)
    show_banner = background if banner is None else bool(banner)
    if store is not None:
        store.subscribe(bus.publish)

    def caller(request):
        """The Principal behind a request, None when it is unpaired, or an access.Refusal when it
        offers a key the wall cannot honour. A request that offers a key is judged on the key
        alone: a wrong key never falls back to the cookie."""
        state = store.current()
        key = request.headers.get("x-wall-key", "")
        if key:
            return access.from_key(key, wall_key, request.headers.get("x-wall-actor", ""), state)
        return access.from_cookie(request.cookies.get(COOKIE, ""), state)

    async def healthz(request):
        return JSONResponse({"ok": True, "revision": store.revision})

    async def state(request):
        who = caller(request)
        refused = _refused(who)
        if refused is not None:
            return refused
        view = await run_in_threadpool(views.state_document, store)
        return JSONResponse(view, headers={"Cache-Control": "no-store"})

    async def stream(request):
        refused = _refused(caller(request))
        if refused is not None:
            return refused
        body = events.stream(bus, lambda: store.revision, is_disconnected=request.is_disconnected)
        return StreamingResponse(body, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def do(request):
        who = caller(request)
        refused = _refused(who)
        if refused is not None:
            return refused
        if who.role == "agent" and not request.headers.get("x-wall-actor", "").strip():
            return _error(400, "Agent calls require X-Wall-Actor header.")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _error(415, "Send JSON.")
        if len(await request.body()) > API_JSON_MAX_BYTES:
            return _error(413, "JSON request is too large.")
        try:
            body = await request.json()
        except (ValueError, UnicodeError, RecursionError):
            return _error(400, "That wasn't valid JSON.")
        if not isinstance(body, dict) or set(body) != {"op", "args"}:
            return _error(400, 'Send {"op": ..., "args": {...}}.')
        name, args = body.get("op"), body.get("args")
        if not isinstance(name, str) or not name.strip() or not isinstance(args, dict):
            return _error(400, 'Send {"op": "name", "args": {...}}.')
        spec = ops.OPS.get(name)
        if spec is None:
            return _error(404, f"Unknown op {name!r}.")
        if who.role not in spec.roles:
            return _error(403, "Ask a parent to do that.")
        try:
            if spec.in_transaction:
                result = await run_in_threadpool(
                    store.transact, lambda draft, now: spec.fn(draft, args, who, now, ctx), kinds=spec.kinds)
            else:
                result = await run_in_threadpool(spec.fn, store, args, who, ctx)
        except engine.WallError as exc:
            return _error(400, str(exc))
        return JSONResponse({"ok": True, "result": result, "revision": store.revision})

    async def pair(request):
        """Trade a pairing code for the device cookie. Wrong codes are counted per client address
        (access.Pairing) and refused with 429 once there have been too many."""
        if len(await request.body()) > API_JSON_MAX_BYTES:
            return _error(413, "JSON request is too large.")
        try:
            body = await request.json()
        except (ValueError, UnicodeError, RecursionError):
            body = None
        if not isinstance(body, dict):
            return _error(400, "Send the code as JSON.")
        client_ip = request.client.host if request.client else "unknown"
        try:
            grant = ctx.pairing.redeem(body.get("code"), ip=client_ip)
        except PermissionError as exc:
            return _error(429, str(exc))
        if grant is None:
            return _error(403, "That code didn't work. Codes last ten minutes and work once.")
        token, device = await run_in_threadpool(
            store.transact, lambda draft, now: access.add_device(draft, grant, str(body.get("name") or ""), now),
            kinds=("devices",))
        response = JSONResponse({"ok": True, "device": device})
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        # Lax, not strict: the tablet reaches the wall from a home-screen shortcut, which is a
        # top-level navigation the cookie must accompany. HttpOnly keeps it out of the app's scripts.
        response.set_cookie(COOKIE, token, max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax",
                            secure=secure)
        return response

    async def photo(request):
        refused = _refused(caller(request))
        if refused is not None:
            return refused
        name = request.path_params["name"]
        path = os.path.join(photo_dir, name)
        if not PHOTO_NAME.fullmatch(name) or not os.path.isfile(path):
            return _error(404, "No such photo.")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    async def video(request):
        refused = _refused(caller(request))
        if refused is not None:
            return refused
        name = request.path_params["name"]
        path = os.path.join(ctx.video_dir, name)
        if not VIDEO_NAME.fullmatch(name) or not os.path.isfile(path):
            return _error(404, "No such video.")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    async def intercom_audio(request):
        refused = _refused(caller(request))
        if refused is not None:
            return refused
        name = request.path_params["name"]
        path = os.path.join(ctx.intercom_dir, name)
        if not INTERCOM_NAME.fullmatch(name) or not os.path.isfile(path):
            return _error(404, "No such intercom audio.")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=900"})

    async def intercom_upload(request):
        """A clip recorded on a tablet, stored under a content hash until send_intercom's message expires."""
        refused = _refused(caller(request))
        if refused is not None:
            return refused
        body = await request.body()
        if not body or len(body) > INTERCOM_MAX_BYTES:
            return _error(400, "Intercom audio must be between 1 byte and 512 KB.")
        mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        name = hashlib.sha256(body + os.urandom(8)).hexdigest()[:16] + INTERCOM_EXTENSIONS.get(mime, ".webm")

        def save():
            os.makedirs(ctx.intercom_dir, exist_ok=True)
            with open(os.path.join(ctx.intercom_dir, name), "wb") as handle:
                handle.write(body)
        await run_in_threadpool(save)
        return JSONResponse({"ok": True, "file": name, "url": f"/intercom/{name}"})

    async def video_reply_upload(request):
        """A kid's recorded reply to a 24-hour video message: the clip is sent straight on to the
        parent's Telegram chat and the reply is noted on the message itself. Nothing is kept on disk."""
        who = caller(request)
        refused = _refused(who)
        if refused is not None:
            return refused
        video_id = (request.query_params.get("id") or request.headers.get("x-video-id") or "").strip()
        if not video_id:
            return _error(400, "Say which video message this replies to (?id=...).")
        body = await request.body()
        if not body or len(body) > VIDEO_REPLY_MAX_BYTES:
            return _error(400, "Video reply must be between 1 byte and 16 MB.")
        mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower() or "video/webm"
        try:
            result = await run_in_threadpool(
                store.transact,
                lambda draft, now: ops.record_video_reply(draft, video_id, who, now, video_bytes=body, mime=mime),
                kinds=("notes", "outbox"))
        except engine.WallError as exc:
            return _error(400, str(exc))
        return JSONResponse({"ok": True, "result": result, "revision": store.revision})

    def tidy(draft, now):
        """Start-up and nightly housekeeping: old stars to the archive, old history out, and expired
        24-hour video messages and fifteen-minute intercom messages removed along with their files."""
        store.archive(engine.compact_ledger(draft, now))
        stamp = now.isoformat()
        # Only touch the draft when something expired: an untouched draft keeps the revision unchanged.
        videos = draft.get("video_messages") or []
        live = lambda v: isinstance(v, dict) and str(v.get("expires_at") or "") > stamp
        expired = [v for v in videos if not live(v)]
        if expired:
            surviving = [v for v in videos if live(v)]
            kept_files = {v.get("file") for v in surviving}
            draft["video_messages"] = surviving
            for item in expired:
                name = item.get("file") if isinstance(item, dict) else None
                if isinstance(name, str) and name not in kept_files and VIDEO_NAME.fullmatch(name) and ctx.video_dir:
                    with contextlib.suppress(OSError):
                        os.unlink(os.path.join(ctx.video_dir, name))
        messages = draft.get("intercom_messages") or []
        stale = [m for m in messages if not live(m)]
        if stale:
            surviving = [m for m in messages if live(m)]
            kept_audio = {m.get("audio_file") for m in surviving}
            draft["intercom_messages"] = surviving
            for item in stale:
                name = item.get("audio_file") if isinstance(item, dict) else None
                if (isinstance(name, str) and name not in kept_audio and INTERCOM_NAME.fullmatch(name)
                        and ctx.intercom_dir):
                    with contextlib.suppress(OSError):
                        os.unlink(os.path.join(ctx.intercom_dir, name))
        # An upload whose send never came (or was evicted without a tidy) gets the same fifteen-minute grace.
        kept_audio = {m.get("audio_file") for m in (draft.get("intercom_messages") or [])
                      if isinstance(m, dict) and m.get("audio_file")}
        if ctx.intercom_dir and os.path.isdir(ctx.intercom_dir):
            cutoff = (now - timedelta(minutes=INTERCOM_TTL_MINUTES)).timestamp()
            for entry in os.listdir(ctx.intercom_dir):
                if INTERCOM_NAME.fullmatch(entry) and entry not in kept_audio:
                    path = os.path.join(ctx.intercom_dir, entry)
                    with contextlib.suppress(OSError):
                        if os.path.getmtime(path) <= cutoff:
                            os.unlink(path)
        return engine.maintain(draft, now)

    async def midnight():
        while True:
            await asyncio.sleep(seconds_to_midnight(store.now()))
            await run_in_threadpool(store.transact, tidy, kinds=("day",))
            bus.publish({"revision": store.revision, "kinds": ["day"]})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        nonlocal store
        ctx.loop = asyncio.get_running_loop()
        running = []
        threads = []
        stop_weather = threading.Event()
        stop_news = threading.Event()
        try:
            if store is None:
                store = Store()
                ctx.store = store
                store.subscribe(bus.publish)
            bus.bind(ctx.loop)
            await run_in_threadpool(store.transact, tidy, kinds=("day",))
            if show_banner:
                try:
                    ctx.banner = startup_banner(ctx.pairing, public_url=os.environ.get("PCWALL_PUBLIC_URL"))
                except Exception as exc:       # the banner is a courtesy: never keep the wall from starting
                    ctx.logger.warning("Startup banner could not be shown: %s", exc)
            if background:
                settings = store.current().get("settings", {})
                weather_settings = settings.get("weather") or {}
                if weather_settings.get("enabled", True):
                    try:
                        weather_client = weather.OpenMeteo(
                            lat=weather_settings.get("lat", os.environ.get("PCWALL_LAT")),
                            lon=weather_settings.get("lon", os.environ.get("PCWALL_LON")),
                            city=weather_settings.get("city") or os.environ.get("PCWALL_CITY", "Springfield"),
                            units=weather_settings.get("units") or os.environ.get("PCWALL_WEATHER_UNITS", "fahrenheit"),
                        )
                        refresh_mins = int(weather_settings.get("refresh_minutes", 15))
                        worker = threading.Thread(target=weather.weather_loop,
                                                  args=(store, weather_client, refresh_mins, stop_weather),
                                                  daemon=True)
                        worker.start()
                        threads.append(worker)
                    except (TypeError, ValueError, Exception) as exc:
                        ctx.logger.warning("Weather loop could not start: %s", exc)
                if (settings.get("news") or {}).get("enabled", True):
                    worker = threading.Thread(target=news.news_loop,
                                              args=(store, lambda: store.current().get("settings", {}), stop_news),
                                              daemon=True)
                    worker.start()
                    threads.append(worker)
                running.append(asyncio.create_task(midnight()))
                for task in tasks:
                    running.append(asyncio.create_task(task(app)))
            yield
        finally:
            stop_weather.set()
            stop_news.set()
            for task in running:
                task.cancel()
            try:
                await asyncio.gather(*running, return_exceptions=True)
                for worker in threads:
                    await run_in_threadpool(worker.join, 2)
            finally:
                bus.bind(None)
                ctx.loop = None

    async def http_error(request, exc):
        return _error(exc.status_code, str(exc.detail))

    routes = [
        Route("/healthz", healthz),
        Route("/api/state", state),
        Route("/api/events", stream),
        Route("/api/do", do, methods=["POST"]),
        Route("/api/pair", pair, methods=["POST"]),
        Route("/photos/{name}", photo),
        Route("/videos/{name}", video),
        Route("/intercom/{name}", intercom_audio),
        Route("/api/intercom/upload", intercom_upload, methods=["POST"]),
        Route("/api/video-reply", video_reply_upload, methods=["POST"]),
    ]
    routes.append(Mount("/", app=_StaticFiles(directory=static_dir, html=True, check_dir=False)))
    app = Starlette(routes=routes, lifespan=lifespan, max_body_size=VIDEO_REPLY_MAX_BYTES,
                    exception_handlers={HTTPException: http_error})
    app.state.ctx = ctx
    return app


# Store construction is deferred to lifespan startup: importing the ASGI app is read-only.
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("PCWALL_HOST", "0.0.0.0"),
                port=int(os.environ.get("PCWALL_PORT", DEFAULT_PORT)),
                log_level="warning", timeout_graceful_shutdown=5)

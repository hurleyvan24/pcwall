"""Focused API, background-worker, and weather/news display contracts."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone
import asyncio
import concurrent.futures
import importlib
import json
import queue
import re
import shutil
import subprocess
import threading
import time

import pytest
from starlette.testclient import TestClient

from pcwall import access, config, news
from pcwall.store import Store


STAMP = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    family = config.Family(
        schema=1, name="Test family", timezone="UTC", school=config.SchoolRules(),
        members=[
            {"id": "parent", "name": "Parent", "role": "parent", "color": "blue", "emoji": "P"},
            {"id": "kid", "name": "Kid", "role": "kid", "color": "green", "emoji": "K",
             "birthdate": "2018-01-01"},
        ],
        routines=[], overrides={}, rewards=[], parent_pin="9876",
    )
    return Store(tmp_path / "wall.json", family=family, clock=lambda: STAMP)


def test_healthz(store):
    server = importlib.import_module("pcwall.server")
    with TestClient(server.create_app(store, wall_key="test-key", background=False)) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "revision": store.revision}


def test_api_state(store):
    server = importlib.import_module("pcwall.server")
    with TestClient(server.create_app(store, wall_key="test-key", background=False)) as client:
        assert client.get("/api/state").status_code == 401
        response = client.get("/api/state", headers={"x-wall-key": "test-key"})
        assert response.status_code == 200
        payload = response.json()
        assert {"revision", "today", "settings", "weather", "news"} <= payload.keys()
        assert "parent_pin" not in json.dumps(payload)


@pytest.mark.parametrize("weather_enabled,news_enabled,background", [
    (True, True, True), (False, False, True), (True, False, True),
    (False, True, True), (True, True, False),
])
def test_background_lifecycle(store, monkeypatch, weather_enabled, news_enabled, background):
    server = importlib.import_module("pcwall.server")
    workers = {}
    ready = {name: threading.Event() for name in ("weather", "news")}

    def weather_worker(actual_store, client, refresh_minutes, stop):
        workers["weather"] = (threading.current_thread(), stop, client, refresh_minutes)
        ready["weather"].set()
        stop.wait(5)

    def news_worker(actual_store, reader, stop):
        workers["news"] = (threading.current_thread(), stop, reader)
        ready["news"].set()
        stop.wait(5)

    monkeypatch.setattr(server.weather, "weather_loop", weather_worker)
    monkeypatch.setattr(server.news, "news_loop", news_worker)
    store.transact(lambda draft, now: draft["settings"].update({
        "weather": {"enabled": weather_enabled, "lat": 34.05, "lon": -118.25,
                    "units": "celsius", "refresh_minutes": 7},
        "news": {"enabled": news_enabled},
    }))
    expected = {name for name, enabled in (("weather", weather_enabled), ("news", news_enabled))
                if background and enabled}
    app = server.create_app(store, wall_key="test-key", background=background)
    with TestClient(app):
        assert all(ready[name].wait(2) for name in expected)
        assert set(workers) == expected
        assert all(worker[0].daemon and worker[0].is_alive() for worker in workers.values())
        if "weather" in expected:
            client = workers["weather"][2]
            assert (client.lat, client.lon, client.units, workers["weather"][3]) == (34.05, -118.25, "celsius", 7)
        if "news" in expected:
            store.transact(lambda draft, now: draft["settings"].update(marker="fresh"))
            assert workers["news"][2]()["marker"] == "fresh"
    assert all(worker[1].is_set() for worker in workers.values())
    assert all(not worker[0].is_alive() for worker in workers.values())


@pytest.mark.parametrize("weather_settings,client_error", [
    ({"units": "kelvin", "refresh_minutes": "bad"}, None),
    ({"refresh_minutes": "bad"}, None),
    ({"refresh_minutes": None}, None),
    ({"lat": "invalid"}, None),
    ({}, RuntimeError("weather client unavailable")),
])
def test_lifespan_tolerates_invalid_weather_settings(
        store, monkeypatch, caplog, weather_settings, client_error):
    server = importlib.import_module("pcwall.server")
    weather_started = threading.Event()
    news_ready = threading.Event()
    workers = []

    def weather_worker(*args):
        weather_started.set()

    def news_worker(actual_store, reader, stop):
        workers.append((threading.current_thread(), stop))
        news_ready.set()
        stop.wait(5)

    if client_error is not None:
        def fail_client(**kwargs):
            raise client_error
        monkeypatch.setattr(server.weather, "OpenMeteo", fail_client)
    monkeypatch.setattr(server.weather, "weather_loop", weather_worker)
    monkeypatch.setattr(server.news, "news_loop", news_worker)
    store.transact(lambda draft, now: draft["settings"].update({
        "weather": {"enabled": True, "lat": 34.05, "lon": -118.25,
                    "units": "celsius", **weather_settings},
        "news": {"enabled": True},
    }))
    app = server.create_app(store, wall_key="test-key")
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert news_ready.wait(2)
        assert not weather_started.is_set()
        assert any(record.name == server.__name__ and record.levelname == "WARNING"
                   and "weather" in record.getMessage().lower() for record in caplog.records)
    assert all(stop.is_set() for _, stop in workers)
    assert all(not worker.is_alive() for worker, _ in workers)


def test_non_ascii_headlines_remain_distinct():
    titles = ["東京 の朝", "京都 の朝", "  東京   の朝  ", "Καλημέρα", "ΚΑΛΗΜΈΡΑ"]
    headlines = [news.Headline(title, "", "", "Local", str(i)) for i, title in enumerate(titles)]
    assert [headline.title for headline in news.merge([headlines])] == ["東京 の朝", "京都 の朝", "Καλημέρα"]


@pytest.mark.parametrize("failure", ["settings", "fetch", "transaction"])
def test_news_recovers_and_publishes_transaction(store, monkeypatch, failure):
    class TwoCycles(threading.Event):
        def __init__(self):
            super().__init__()
            self.cycles = 0

        def wait(self, timeout=None):
            self.cycles += 1
            if self.cycles == 2:
                self.set()
            return self.is_set()

    failed = False
    settings = {"news": {"feeds": [{"title": "Local", "url": "https://example.test/rss"}]}}

    def maybe_fail(stage):
        nonlocal failed
        if stage == failure and not failed:
            failed = True
            raise RuntimeError("transient " + stage)

    def reader():
        maybe_fail("settings")
        return settings

    def fetch(url):
        maybe_fail("fetch")
        return b"<rss><channel><item><title>Local news</title></item></channel></rss>"

    transact = store.transact

    def publish(change, *, kinds=()):
        maybe_fail("transaction")
        return transact(change, kinds=kinds)

    notifications = []
    store.subscribe(notifications.append)
    monkeypatch.setattr(news, "POLL_SECONDS", 1)
    monkeypatch.setattr(news, "fetch_feed", fetch)
    monkeypatch.setattr(store, "transact", publish)
    news.news_loop(store, reader, TwoCycles())
    result = store.current()["news"]
    assert [headline["title"] for headline in result["headlines"]] == ["Local news"]
    assert result["items"] == result["headlines"]
    assert (result["updated_at"], result["sources"], notifications[-1]["kinds"]) == (STAMP.isoformat(), ["Local"], ["news"])


def test_news_publishes_to_update_only_store(monkeypatch):
    class UpdateStore:
        def __init__(self):
            self.document = {"keep": {"value": 1}, "news": {"headlines": []}}

        def update(self, change):
            change(self.document)

    class OneCycle(threading.Event):
        def wait(self, timeout=None):
            self.set()
            return True

    store = UpdateStore()
    monkeypatch.setattr(news, "POLL_SECONDS", 1)
    monkeypatch.setattr(news, "fetch_feed", lambda url:
                        b"<rss><channel><item><title>Local news</title>"
                        b"<link>https://example.test/story</link></item></channel></rss>")
    before = datetime.now(timezone.utc)
    news.news_loop(store, lambda: {"news": {"feeds": [
        {"title": "Local", "url": "https://example.test/rss"}]}}, OneCycle())
    result = store.document["news"]
    assert [headline["title"] for headline in result["headlines"]] == ["Local news"]
    assert result["items"] == result["headlines"]
    assert result["headlines"][0]["link"] == "https://example.test/story"
    assert result["sources"] == ["Local"]
    assert before <= datetime.fromisoformat(result["updated_at"]) <= datetime.now(timezone.utc)
    assert store.document["keep"] == {"value": 1}


def test_weather_chip_and_ticker_render_current_documents():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the browser rendering contract")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('pcwall/static/app.mjs', 'utf8');
const functions = ['escapeHtml', 'list', 'weatherDays', 'weatherValue', 'weatherSky', 'renderSidebarWeather', 'renderTicker'];
const declarations = functions.map(name => {
  const start = source.indexOf('function ' + name + '(');
  const end = source.indexOf('\nfunction ', start + 1);
  return source.slice(start, end);
}).join('\n');
const ticker = {dataset: {}, hidden: true, innerHTML: ''};
const context = {state: {}, clock: () => ({day: '2026-11-02'}), $: () => ticker};
vm.createContext(context);
vm.runInContext(declarations, context);
const report = {};
context.state = {weather: {temperature: 0, condition: '<Cloudy>', emoji: '☁', units: '°C'}};
let chip = context.renderSidebarWeather();
report.temperature = chip.includes('0°C') && chip.includes('&lt;Cloudy&gt;') && chip.includes('☁');
context.state = {weather: {temp: 72, condition: 'Sunny', emoji: '☀', units: {temp: '°F'}}};
chip = context.renderSidebarWeather();
report.temp = chip.includes('72°F') && chip.includes('Sunny');
context.state = {weather: {current: {time: '2026-11-02T12:00', temperature_2m: 18, weather_code: 3}, units: {temp: '°C'}}};
report.current = context.renderSidebarWeather().includes('Overcast');
context.state = {weather: {current: {temperature: 0, emoji: '❄', label: '<Freezing>'}, temp: 72, temperature: 71, emoji: '☀', condition: 'Legacy sunny', units: 'celsius'}};
chip = context.renderSidebarWeather();
report.currentTemperature = chip.includes('0°C') && !chip.includes('72°C');
report.currentEmoji = chip.includes('❄') && !chip.includes('☀');
report.currentLabel = chip.includes('&lt;Freezing&gt;') && !chip.includes('Legacy sunny');
context.state = {weather: {current: {temperature: null, emoji: null, label: null}, temperature: 4, emoji: '☁', condition: 'Cloudy', units: '°C'}};
chip = context.renderSidebarWeather();
report.currentFallback = chip.includes('4°C') && chip.includes('☁') && chip.includes('Cloudy');
context.state = {weather: {}};
report.unavailable = context.renderSidebarWeather().includes('unavailable');
context.state = {news: {headlines: [{title: '<Local news>', source: 'Town'}]}, settings: {news: {ticker_speed: 45}}};
context.renderTicker();
report.headlines = !ticker.hidden && ticker.innerHTML.includes('&lt;Local news&gt;') && ticker.innerHTML.includes('Town') && ticker.innerHTML.includes('--ticker-duration:45s');
context.state.settings.news.ticker_speed = 75;
context.renderTicker();
report.speed = ticker.innerHTML.includes('--ticker-duration:75s');
context.state = {news: {items: [{title: 'Compatibility news'}]}};
context.renderTicker();
report.items = ticker.innerHTML.includes('Compatibility news') && ticker.innerHTML.includes('--ticker-duration:60s');
context.state = {news: [{text: 'Legacy news'}]};
context.renderTicker();
report.legacy = ticker.innerHTML.includes('Legacy news');
context.state = {news: {headlines: []}};
context.renderTicker();
report.empty = ticker.hidden && ticker.innerHTML === '';
console.log(JSON.stringify(report));
"""
    result = subprocess.run([node, "-"], input=script, text=True, encoding="utf-8",
                            cwd=pathlib.Path(__file__).resolve().parent.parent,
                            capture_output=True, check=True)
    checks = json.loads(result.stdout)
    assert all(checks.values()), checks


# ── Wave 6.1: access roles, key auth and pairing ────────────────────────────

AGENT = {"x-wall-key": "test-key", "x-wall-actor": "parent"}


class Clock:
    """A monotonic clock the tests move by hand."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def do(client, op, args=None, **kwargs):
    return client.post("/api/do", json={"op": op, "args": args or {}}, **kwargs)


def pair_cookie(client, pairing, role, member=None, name="screen"):
    """Pair a device through /api/pair and return its cookie as a header, leaving the client's jar empty."""
    server = importlib.import_module("pcwall.server")
    code = pairing.issue(role, member)["code"]
    response = client.post("/api/pair", json={"code": code, "name": name})
    assert response.status_code == 200, response.text
    token = response.cookies[server.COOKIE]
    client.cookies.clear()
    return {"cookie": f"{server.COOKIE}={token}"}


def wrong_code(pairing):
    """A six-digit code that is not live."""
    return next(code for code in ("000000", "000001") if code not in pairing.codes)


def test_principal_actor_follows_identity(store):
    state = store.current()
    assert access.Principal("kiosk").actor == "wall"
    assert access.Principal("parent", "parent", "dv_1", "Phone").actor == "parent"
    assert access.Principal("agent").actor == "agent"
    assert access.from_key("k", "k", "parent", state).member == "parent"
    assert access.from_key("k", "k", " HomeAgent ", state).actor == "HomeAgent"
    assert access.from_key("k", "k", "<script>", state).actor == "agent"
    assert access.from_key("k", "other", "parent", state) is None
    assert access.from_key("", "k", "parent", state) is None
    assert access.from_key("k", "", "parent", state) is access.AGENT_CALLS_DISABLED


def test_pair_sets_httponly_lax_cookie(store):
    server = importlib.import_module("pcwall.server")
    pairing = access.Pairing(Clock())
    with TestClient(server.create_app(store, wall_key="test-key", background=False, pairing=pairing)) as client:
        code = pairing.issue("kiosk")["code"]
        response = client.post("/api/pair", json={"code": code, "name": "Kitchen"})
        assert response.status_code == 200
        device = response.json()["device"]
        assert (device["role"], device["name"]) == ("kiosk", "Kitchen") and "hash" not in device
        attributes = {part.strip().partition("=")[0].lower(): part.strip().partition("=")[2]
                      for part in response.headers["set-cookie"].split(";")}
        token = attributes[server.COOKIE]
        assert token and "httponly" in attributes and "secure" not in attributes
        assert attributes["samesite"].lower() == "lax"
        assert attributes["max-age"] == str(400 * 86400)
        assert attributes["path"] == "/"
        client.cookies.clear()
        # The cookie is the screen's credential, stored hashed; the code is spent.
        assert client.get("/api/state", headers={"cookie": f"{server.COOKIE}={token}"}).status_code == 200
        assert client.post("/api/pair", json={"code": code}).status_code == 403
        assert token not in json.dumps(store.current())
        assert store.current()["devices"][0]["hash"] == access.token_hash(token)
        # Behind TLS the cookie is also Secure.
        code = pairing.issue("parent", "parent")["code"]
        response = client.post("/api/pair", json={"code": code}, headers={"x-forwarded-proto": "https"})
        assert "secure" in response.headers["set-cookie"].lower()


def test_pair_code_expires_after_ten_minutes(store):
    server = importlib.import_module("pcwall.server")
    clock = Clock()
    pairing = access.Pairing(clock)
    with TestClient(server.create_app(store, wall_key="test-key", background=False, pairing=pairing)) as client:
        early, late = pairing.issue("kiosk")["code"], pairing.issue("kiosk")["code"]
        clock.advance(9 * 60)
        assert client.post("/api/pair", json={"code": early}).status_code == 200
        clock.advance(2 * 60)                                        # eleven minutes after issue
        assert client.post("/api/pair", json={"code": late}).status_code == 403
        assert late not in pairing.codes                             # dropped, not merely refused
        assert client.post("/api/pair", json={"code": early}).status_code == 403     # spent codes stay spent
        fresh = pairing.issue("kiosk", seconds=120)["code"]
        clock.advance(121)
        assert client.post("/api/pair", json={"code": fresh}).status_code == 403     # a shorter life is honoured
        assert len(store.current()["devices"]) == 1


def test_pair_rate_limited_after_five_per_minute(store):
    server = importlib.import_module("pcwall.server")
    clock = Clock()
    pairing = access.Pairing(clock)
    app = server.create_app(store, wall_key="test-key", background=False, pairing=pairing)
    with TestClient(app, client=("192.0.2.10", 40000)) as client:
        good = pairing.issue("kiosk")["code"]
        bad = wrong_code(pairing)
        for _ in range(5):
            assert client.post("/api/pair", json={"code": bad}).status_code == 403
        blocked = client.post("/api/pair", json={"code": good})
        assert blocked.status_code == 429
        assert "one minute" in blocked.json()["error"]
        assert good in pairing.codes                                 # a refused try never spends a good code
        # Another address is unaffected; the window is one minute; stale addresses are forgotten.
        neighbour = TestClient(app, client=("192.0.2.11", 40000))
        assert neighbour.post("/api/pair", json={"code": bad}).status_code == 403
        clock.advance(59)
        assert client.post("/api/pair", json={"code": good}).status_code == 429
        clock.advance(2)
        assert client.post("/api/pair", json={"code": good}).status_code == 200
        assert "192.0.2.11" not in pairing.limiters


def test_startup_prints_lan_url_and_code(store, monkeypatch, capsys):
    server = importlib.import_module("pcwall.server")
    for name in ("PCWALL_HOST", "PCWALL_PORT", "PCWALL_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    pairing = access.Pairing(Clock())
    url, code = server.startup_banner(pairing, host="192.0.2.10", port=8770)
    printed = capsys.readouterr().out
    assert url == "http://192.0.2.10:8770/" and url in printed
    assert re.fullmatch(r"\d{6}", code) and code in printed
    assert pairing.redeem(code, ip="console")["role"] == "kiosk"
    # A parent code is printed beside it, so a phone can be paired from the console without an agent.
    parent_code = re.search(r"phone with code (\d{6}) \(parent, valid 10 minutes\)", printed).group(1)
    assert parent_code != code
    grant = pairing.redeem(parent_code, ip="console")
    assert (grant["role"], grant["member"]) == ("parent", None)
    # A wildcard host becomes the LAN address; a public URL wins outright; a bad port falls back.
    monkeypatch.setattr(server, "_lan_ip", lambda: "192.0.2.20")
    assert server.startup_banner(pairing, host="0.0.0.0", port="8771")[0] == "http://192.0.2.20:8771/"
    assert server.startup_banner(pairing, host="", port="x")[0] == "http://192.0.2.20:8770/"
    assert server.startup_banner(pairing, public_url="https://wall.example.test")[0] == "https://wall.example.test/"
    # The server shows it on startup, with the address it was configured to listen on.
    monkeypatch.setenv("PCWALL_HOST", "192.0.2.30")
    monkeypatch.setenv("PCWALL_PORT", "8772")
    capsys.readouterr()
    app = server.create_app(store, wall_key="test-key", background=False, pairing=pairing, banner=True)
    with TestClient(app):
        url, code = app.state.ctx.banner
        shown = capsys.readouterr().out
        assert url == "http://192.0.2.30:8772/" and url in shown
        assert pairing.redeem(code, ip="console")["role"] == "kiosk"
        parent_code = re.search(r"phone with code (\d{6}) \(parent", shown).group(1)
        assert pairing.redeem(parent_code, ip="console")["role"] == "parent"
    quiet = server.create_app(store, wall_key="test-key", background=False, pairing=pairing)
    with TestClient(quiet):
        assert quiet.state.ctx.banner is None
    assert "pcwall is ready" not in capsys.readouterr().out           # tests and embedders stay quiet


@pytest.mark.parametrize("role", ["kiosk", "parent", "agent"])
def test_role_matrix(store, role):
    """Who may call what: everyone's ops for all, parents' ops for phones and the agent only."""
    server = importlib.import_module("pcwall.server")
    pairing = access.Pairing(Clock())
    expected = {                        # op: (args, kiosk, parent, agent)
        "stars": ({}, 200, 200, 200),
        "award_stars": ({"who": "kid", "amount": 1, "reason": "matrix"}, 403, 200, 200),
        "status": ({}, 403, 200, 200),
        "pairing_code": ({"role": "kiosk"}, 403, 200, 200),
        "settings_set": ({"night": {"start": "20:30"}}, 403, 200, 200),
        "revoke_device": ({"device": "dv_missing"}, 403, 400, 400),
        "no_such_op": ({}, 404, 404, 404),
    }
    column = ("kiosk", "parent", "agent").index(role)
    with TestClient(server.create_app(store, wall_key="test-key", background=False, pairing=pairing)) as client:
        if role == "agent":
            headers = AGENT
        else:
            headers = pair_cookie(client, pairing, role, "parent" if role == "parent" else None)
        for op, (args, *statuses) in expected.items():
            response = do(client, op, args, headers=headers)
            assert response.status_code == statuses[column], (op, response.text)
            if response.status_code == 403:
                assert response.json() == {"ok": False, "error": "Ask a parent to do that."}
        assert do(client, "stars").status_code == 401                # unpaired callers get nothing


def test_kiosk_cannot_award_stars(store):
    server = importlib.import_module("pcwall.server")
    pairing = access.Pairing(Clock())
    with TestClient(server.create_app(store, wall_key="test-key", background=False, pairing=pairing)) as client:
        kiosk = pair_cookie(client, pairing, "kiosk")
        before = do(client, "stars", {"history": 1}, headers=kiosk).json()
        response = do(client, "award_stars", {"who": "kid", "amount": 3, "reason": "nope"}, headers=kiosk)
        assert response.status_code == 403
        assert response.json() == {"ok": False, "error": "Ask a parent to do that."}
        assert do(client, "stars", {"history": 1}, headers=kiosk).json() == before
        assert store.revision == before["revision"]
        parent = pair_cookie(client, pairing, "parent", "parent")
        revision = store.revision
        response = do(client, "award_stars", {"who": "kid", "amount": 3, "reason": "yes"}, headers=parent)
        assert response.status_code == 200, response.text
        assert store.revision > revision


def test_key_requires_actor_header(store):
    server = importlib.import_module("pcwall.server")
    pairing = access.Pairing(Clock())
    with TestClient(server.create_app(store, wall_key="test-key", background=False, pairing=pairing)) as client:
        key_only = {"x-wall-key": "test-key"}
        response = do(client, "stars", headers=key_only)
        assert (response.status_code, response.json()["error"]) == (400, "Agent calls require X-Wall-Actor header.")
        assert do(client, "stars", headers={**key_only, "x-wall-actor": "   "}).status_code == 400
        assert do(client, "stars", headers=AGENT).status_code == 200
        assert do(client, "stars", headers={**key_only, "x-wall-actor": "HomeAgent"}).status_code == 200
        assert client.get("/api/state", headers=key_only).status_code == 200      # reads need no actor
        # A wrong key is unpaired, and never downgrades to a cookie that happens to be present.
        kiosk = pair_cookie(client, pairing, "kiosk")
        assert do(client, "stars", headers={"x-wall-key": "wrong", "x-wall-actor": "parent"}).status_code == 401
        assert do(client, "stars", headers={"x-wall-key": "wrong", **kiosk}).status_code == 401
        assert do(client, "stars", headers=kiosk).status_code == 200


def test_blank_key_returns_agent_calls_disabled(store, monkeypatch, tmp_path):
    server = importlib.import_module("pcwall.server")
    pairing = access.Pairing(Clock())
    refused = {"ok": False, "error": "agent_calls_disabled"}
    with TestClient(server.create_app(store, wall_key="", background=False, pairing=pairing)) as client:
        response = client.get("/api/state", headers=AGENT)
        assert (response.status_code, response.json()) == (403, refused)
        response = do(client, "stars", headers=AGENT)
        assert (response.status_code, response.json()) == (403, refused)
        assert client.get("/api/events", headers=AGENT).status_code == 403
        assert client.get("/api/state").status_code == 401                        # no key offered: just unpaired
        assert client.get("/api/state", headers={"x-wall-key": ""}).status_code == 401
        kiosk = pair_cookie(client, pairing, "kiosk")
        assert client.get("/api/state", headers=kiosk).status_code == 200          # paired screens keep working
    # The same when PCWALL_KEY is blank in the environment and there is no key file on disk.
    monkeypatch.setenv("PCWALL_KEY", "  ")
    monkeypatch.setattr(server, "ROOT", str(tmp_path))
    with TestClient(server.create_app(store, background=False, pairing=pairing)) as client:
        response = client.get("/api/state", headers={"x-wall-key": "anything"})
        assert (response.status_code, response.json()) == (403, refused)
    # A blank PCWALL_KEY is the operator's word: a key file left on disk is never consulted.
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "pcwall_key.txt").write_text("old-test-key\n", encoding="utf-8")
    monkeypatch.setenv("PCWALL_KEY", "")
    stale = {"x-wall-key": "old-test-key", "x-wall-actor": "parent"}
    revision = store.revision
    with TestClient(server.create_app(store, background=False, pairing=pairing)) as client:
        response = client.get("/api/state", headers=stale)
        assert (response.status_code, response.json()) == (403, refused)
        response = do(client, "award_stars", {"who": "kid", "amount": 1, "reason": "stale"}, headers=stale)
        assert (response.status_code, response.json()) == (403, refused)
        assert client.get("/api/state").status_code == 401
    assert store.revision == revision                                             # the refused call wrote nothing
    # Only an unset PCWALL_KEY lets the file speak.
    monkeypatch.delenv("PCWALL_KEY", raising=False)
    with TestClient(server.create_app(store, background=False, pairing=pairing)) as client:
        assert client.get("/api/state", headers=stale).status_code == 200
        assert do(client, "stars", headers=stale).status_code == 200
        assert client.get("/api/state", headers={"x-wall-key": "anything"}).status_code == 401
    # A key in the environment outranks the file.
    monkeypatch.setenv("PCWALL_KEY", " env-key ")
    with TestClient(server.create_app(store, background=False, pairing=pairing)) as client:
        assert client.get("/api/state", headers={"x-wall-key": "env-key"}).status_code == 200
        assert client.get("/api/state", headers=stale).status_code == 401


def test_settings_set_cannot_touch_parent_pin_any_role(store):
    server = importlib.import_module("pcwall.server")
    pairing = access.Pairing(Clock())
    before = store.current()["settings"]["parent_pin"]
    attempts = ({"parent_pin": "0000"}, {"settings": {"parent_pin": "0000"}},
                {"night": {"parent_pin": "0000"}}, {"parent_pin_is_sample": True},
                {"settings": {"stars": {"max_award": 9, "parent_pin": "0000"}}})
    with TestClient(server.create_app(store, wall_key="test-key", background=False, pairing=pairing)) as client:
        parent = pair_cookie(client, pairing, "parent", "parent")
        kiosk = pair_cookie(client, pairing, "kiosk")
        for headers in (parent, AGENT):
            for args in attempts:
                response = do(client, "settings_set", args, headers=headers)
                assert response.status_code == 400, (args, response.text)
                assert "set_pin" in response.json()["error"]
        for args in attempts:
            assert do(client, "settings_set", args, headers=kiosk).status_code == 403
        settings = store.current()["settings"]
        assert settings["parent_pin"] == before and settings["parent_pin_is_sample"] is False
        assert settings["stars"]["max_award"] == 5                              # a refused write changes nothing
        assert do(client, "settings_set", {"night": {"start": "20:30"}}, headers=parent).status_code == 200
        assert store.current()["settings"]["night"]["start"] == "20:30"


def test_pin_change_needs_current_pin(store):
    server = importlib.import_module("pcwall.server")
    pairing = access.Pairing(Clock())
    before = store.current()["settings"]["parent_pin"]
    with TestClient(server.create_app(store, wall_key="test-key", background=False, pairing=pairing)) as client:
        parent = pair_cookie(client, pairing, "parent", "parent")
        response = do(client, "set_pin", {"pin": "4321"}, headers=parent)
        assert (response.status_code, response.json()["error"]) == (400, "That PIN isn't right.")
        assert do(client, "set_pin", {"pin": "4321", "current_pin": "0000"}, headers=parent).status_code == 400
        assert do(client, "set_pin", {"pin": "4321", "current_pin": 9876}, headers=AGENT).status_code == 400
        assert store.current()["settings"]["parent_pin"] == before
        kiosk = pair_cookie(client, pairing, "kiosk")
        assert do(client, "set_pin", {"pin": "4321", "current_pin": "9876"}, headers=kiosk).status_code == 403
        response = do(client, "set_pin", {"pin": "4321", "current_pin": "9876"}, headers=AGENT)
        assert response.status_code == 200, response.text
        assert response.json()["result"] == {"pin_set": True, "pin_is_sample": False}
        after = store.current()["settings"]["parent_pin"]
        assert after != before and config.verify_pin("4321", after) and not config.verify_pin("9876", after)
        assert "4321" not in after
        # From now on the new PIN is the one that must be shown.
        assert do(client, "set_pin", {"pin": "5555", "current_pin": "9876"}, headers=parent).status_code == 400
        assert do(client, "set_pin", {"pin": "5555", "current_pin": "4321"}, headers=parent).status_code == 200


def test_state_never_contains_pin_hash(store):
    server = importlib.import_module("pcwall.server")
    pairing = access.Pairing(Clock())
    stored = store.current()["settings"]["parent_pin"]
    salt, digest = stored.split("$")[2:]
    with TestClient(server.create_app(store, wall_key="test-key", background=False, pairing=pairing)) as client:
        screens = (AGENT, pair_cookie(client, pairing, "parent", "parent"), pair_cookie(client, pairing, "kiosk"))
        for headers in screens:
            response = client.get("/api/state", headers=headers)
            assert response.status_code == 200
            for secret in (stored, digest, salt, "parent_pin", "pbkdf2"):
                assert secret not in response.text
            assert "parent_pin" not in response.json()["settings"]
        for headers in screens[:2]:
            response = do(client, "status", headers=headers)
            assert response.status_code == 200
            assert stored not in response.text and digest not in response.text
        assert do(client, "status", headers=screens[2]).status_code == 403


def test_sse_emits_change_after_complete(store):
    server = importlib.import_module("pcwall.server")
    app = server.create_app(store, wall_key="test-key", background=False)
    frames = queue.Queue()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": "/api/events", "raw_path": b"/api/events", "root_path": "",
             "query_string": b"", "headers": [(b"host", b"testserver"), (b"x-wall-key", b"test-key")],
             "client": ("192.0.2.10", 40000), "server": ("testserver", 80)}

    async def receive():
        await asyncio.Event().wait()                 # the screen never hangs up during the test

    async def send(message):
        if message["type"] == "http.response.start":
            frames.put(("start", message["status"], dict(message["headers"])))
        elif message.get("body"):
            frames.put(("body", message["body"]))

    def read_until(marker, timeout=5):
        text, deadline = "", time.monotonic() + timeout
        while marker not in text:
            kind, *rest = frames.get(timeout=max(0.01, deadline - time.monotonic()))
            if kind == "body":
                text += rest[0].decode("utf-8")
        return text

    with TestClient(app) as client:
        stream = client.portal.start_task_soon(app, scope, receive, send)
        try:
            kind, status, headers = frames.get(timeout=5)
            assert (kind, status) == ("start", 200)
            assert headers[b"content-type"].startswith(b"text/event-stream")
            assert headers[b"cache-control"] == b"no-store"
            opening = read_until("event: hello\ndata: ")
            assert "retry: 3000" in opening
            hello = re.search(r"event: hello\ndata: (.*)\n", opening)
            assert json.loads(hello.group(1)) == {"revision": store.revision}
            assert frames.empty()                                   # nothing moves until something changes
            revision = store.revision
            response = do(client, "award_stars", {"who": "kid", "amount": 2, "reason": "sse"}, headers=AGENT)
            assert response.status_code == 200, response.text
            change = read_until("event: change")
            match = re.search(r"id: (\d+)\nevent: change\ndata: (\{.*\})\n\n", change)
            assert match, change
            payload = json.loads(match.group(2))
            assert payload["revision"] == store.revision > revision
            assert int(match.group(1)) == payload["revision"]
            assert "stars" in payload["kinds"]
            assert "kid" not in change.lower()                      # events carry no family data
        finally:
            stream.cancel()
            concurrent.futures.wait([stream], timeout=5)

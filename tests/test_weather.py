"""Focused unit tests for pcwall.weather (Wave 4.1).

Every test hands OpenMeteo a fake fetch callable, so nothing here opens a socket. Covers:
geocoding a city into a Location, the empty-results failure, coordinates skipping the
geocoder entirely, parsing the recorded Open-Meteo payload in tests/fixtures into current,
hourly and daily rows, WeatherError on HTTP errors, network errors, bad JSON and an
oversized reply, every WMO code 0..99 mapping to an (emoji, label) pair, and weather_loop
writing to the store, recording a failed refresh and stopping when told to.
"""
from __future__ import annotations

import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

# The repo root goes on sys.path first, so `python -m pytest tests/test_weather.py` finds the
# pcwall package from any working directory and without PYTHONPATH or an installed package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pcwall import weather  # noqa: E402
from pcwall.weather import (  # noqa: E402
    MAX_PAYLOAD_BYTES,
    TIMEOUT_SECONDS,
    WMO_CONDITIONS,
    Forecast,
    Location,
    OpenMeteo,
    WeatherError,
    condition_for,
    weather_loop,
)

FIXTURE = ROOT / "tests" / "fixtures" / "open_meteo.json"
GEOCODING = {
    "results": [
        {
            "id": 4140963,
            "name": "Washington",
            "latitude": 38.89511,
            "longitude": -77.03637,
            "country": "United States",
            "admin1": "District of Columbia",
            "timezone": "America/New_York",
        }
    ],
    "generationtime_ms": 0.6,
}


# ---------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------

class FakeResponse:
    """The slice of http.client.HTTPResponse that OpenMeteo touches."""

    def __init__(self, body, status: int = 200):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        self._buffer = io.BytesIO(body)
        self.status = status
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def close(self) -> None:
        self.closed = True


def fake_fetch(routes: dict, calls: list):
    """fetch(request, timeout=) that routes by URL substring. A route value that is an
    exception is raised; bytes or JSON-able data become a 200 FakeResponse."""

    def fetch(request, timeout=None):
        assert isinstance(request, urllib.request.Request)
        url = request.full_url
        calls.append({"url": url, "timeout": timeout, "headers": dict(request.header_items())})
        for needle, reply in routes.items():
            if needle in url:
                if isinstance(reply, BaseException):
                    raise reply
                return reply if isinstance(reply, FakeResponse) else FakeResponse(reply)
        raise AssertionError(f"unexpected URL {url}")

    return fetch


class FakeStore:
    """Store.transact(change, *, kinds=()) over a plain dict, remembering every kinds tuple
    and firing `wrote` after each change so a test can react from another thread."""

    def __init__(self):
        self.state = {"revision": 0}
        self.kinds = []
        self.wrote = threading.Event()
        self.on_write = None

    def transact(self, change, *, kinds=()):
        now = datetime(2026, 11, 2, 7, 15, tzinfo=timezone.utc)
        result = change(self.state, now)
        self.state["revision"] += 1
        self.kinds.append(tuple(kinds))
        self.wrote.set()
        if self.on_write is not None:
            self.on_write()
        return result


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.units = "fahrenheit"
        self.location = Location("Custom", 38.8951, -77.0364, "auto")

    def fetch_forecast(self):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else self.outcomes_exhausted()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @staticmethod
    def outcomes_exhausted():
        raise WeatherError("no more scripted outcomes")


def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------------------

def test_geocoding_success():
    calls = []
    client = OpenMeteo(city="Washington", fetch=fake_fetch({"geocoding-api": GEOCODING}, calls))

    location = client.resolve_location()

    assert location == Location("Washington", 38.89511, -77.03637, "America/New_York")
    assert len(calls) == 1 and calls[0]["timeout"] == TIMEOUT_SECONDS
    assert "name=Washington" in calls[0]["url"] and "count=1" in calls[0]["url"]
    # The result is cached: a second resolve does not go back to the geocoder.
    assert client.resolve_location() is location and len(calls) == 1


@pytest.mark.parametrize("payload", [{"generationtime_ms": 0.4}, {"results": []}])
def test_geocoding_not_found(payload):
    client = OpenMeteo(city="Nowhereville", fetch=fake_fetch({"geocoding-api": payload}, []))

    with pytest.raises(WeatherError, match="not found"):
        client.resolve_location()


def test_coordinates_bypass_geocoding():
    def never(request, timeout=None):
        raise AssertionError(f"fetch must not be called for coordinates, got {request.full_url}")

    client = OpenMeteo(lat=38.8951, lon=-77.0364, fetch=never)

    assert client.resolve_location() == Location("Custom", 38.8951, -77.0364, "auto")


# ---------------------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------------------

def test_forecast_parses_current_hourly_daily():
    calls = []
    client = OpenMeteo(
        lat=38.8951, lon=-77.0364, fetch=fake_fetch({"/v1/forecast": fixture_payload()}, calls)
    )

    forecast = client.fetch_forecast()

    url = calls[0]["url"]
    assert "latitude=38.8951" in url and "temperature_unit=fahrenheit" in url
    assert "wind_speed_unit=mph" in url and "timezone=auto" in url
    assert isinstance(forecast, Forecast) and datetime.fromisoformat(forecast.fetched_at).tzinfo

    current = forecast.current
    assert (current["temperature"], current["humidity"], current["wind_speed"]) == (52.3, 71, 8.4)
    assert (current["code"], current["emoji"], current["label"]) == (3, "☁️", "Overcast")
    assert (current["temperature_text"], current["wind_text"]) == ("52°F", "8 mph")
    assert current["is_day"] is True

    assert len(forecast.hourly) == 24
    assert forecast.hourly[0]["time"] == "2026-11-02T07:00"
    assert (forecast.hourly[15]["label"], forecast.hourly[15]["precipitation_probability"]) == ("Rain", 80)

    assert len(forecast.daily) == 7
    first, rainy = forecast.daily[0], forecast.daily[1]
    assert (first["date"], first["weekday"], first["high"], first["low"]) == ("2026-11-02", "Mon", 61.9, 48.2)
    assert (rainy["label"], rainy["emoji"], rainy["precipitation_probability"]) == ("Rain", "🌧️", 80)
    assert forecast.daily[6]["label"] == "Thunderstorm" and first["sunrise"] == "2026-11-02T06:38"


def test_forecast_formats_celsius_units():
    calls = []
    client = OpenMeteo(
        lat=52.52, lon=13.405, units="celsius",
        fetch=fake_fetch({"/v1/forecast": fixture_payload()}, calls),
    )

    forecast = client.fetch_forecast()

    assert "temperature_unit=celsius" in calls[0]["url"] and "wind_speed_unit=kmh" in calls[0]["url"]
    assert forecast.current["temperature_text"] == "52°C" and forecast.current["wind_text"] == "8 km/h"


@pytest.mark.parametrize(
    "reply, message",
    [
        (urllib.error.HTTPError("https://api.open-meteo.com/v1/forecast", 503, "Service Unavailable", {}, None), "503"),
        (urllib.error.URLError("Name or service not known"), "could not reach"),
        (TimeoutError("timed out"), "could not reach"),
        (b"<html>502 Bad Gateway</html>", "invalid JSON"),
        (b"[" + b" " * MAX_PAYLOAD_BYTES + b"]", "exceeds"),
        ({"error": True, "reason": "Latitude must be in range of -90 to 90"}, "Latitude"),
    ],
    ids=["http-503", "url-error", "timeout", "bad-json", "oversized", "api-error"],
)
def test_weather_error_on_network_or_5xx(reply, message):
    client = OpenMeteo(lat=38.8951, lon=-77.0364, fetch=fake_fetch({"/v1/forecast": reply}, []))

    with pytest.raises(WeatherError, match=message):
        client.fetch_forecast()


# ---------------------------------------------------------------------------------------
# WMO codes
# ---------------------------------------------------------------------------------------

def test_wmo_code_coverage():
    for code in range(100):
        condition = condition_for(code)
        assert isinstance(condition, tuple) and len(condition) == 2, code
        emoji, label = condition
        assert emoji and label and isinstance(emoji, str) and isinstance(label, str), code

    assert condition_for(0) == ("☀️", "Clear") and WMO_CONDITIONS[0] == ("☀️", "Clear")
    assert condition_for(45) == condition_for(48) == ("🌫️", "Fog")
    assert [condition_for(c)[1] for c in (55, 57, 65, 67, 77, 82, 86, 99)] == [
        "Drizzle", "Freezing Drizzle", "Rain", "Freezing Rain", "Snow", "Rain Showers",
        "Snow Showers", "Thunderstorm",
    ]
    assert condition_for(None) == condition_for(100) == condition_for("cloudy") == weather.FALLBACK_CONDITION


# ---------------------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------------------

def test_weather_loop_updates_store_and_stops(monkeypatch):
    sleeps = []
    monkeypatch.setattr(weather.time, "sleep", lambda seconds: sleeps.append(seconds))
    forecast = OpenMeteo(lat=38.8951, lon=-77.0364).parse_forecast(fixture_payload(), "2026-11-02T12:15:00+00:00")
    store = FakeStore()
    stop = threading.Event()
    store.on_write = stop.set
    client = FakeClient([forecast])

    weather_loop(store, client, 5, stop)

    saved = store.state["weather"]
    assert client.calls == 1 and store.kinds == [("weather",)] and sleeps == []
    assert saved["current"]["label"] == "Overcast" and len(saved["daily"]) == 7
    assert saved["location"] == {"name": "Custom", "latitude": 38.8951, "longitude": -77.0364, "timezone": "auto"}
    assert (saved["units"], saved["fetched_at"], saved["error"]) == ("fahrenheit", "2026-11-02T12:15:00+00:00", None)

    # A failed refresh keeps the last good forecast and records the error beside it.
    stop.clear()
    client.outcomes = [WeatherError("HTTP 503 from api.open-meteo.com")]
    weather_loop(store, client, 5, stop)
    assert store.state["weather"]["error"] == "HTTP 503 from api.open-meteo.com"
    assert store.state["weather"]["current"]["label"] == "Overcast"


def test_weather_loop_thread_stops_during_sleep(monkeypatch):
    real_sleep = time.sleep
    monkeypatch.setattr(weather.time, "sleep", lambda seconds: real_sleep(0.001))
    store = FakeStore()
    stop = threading.Event()
    client = FakeClient([Forecast({"label": "Clear"}, [], [], "2026-11-02T12:00:00+00:00")])
    worker = threading.Thread(target=weather_loop, args=(store, client, 60, stop), daemon=True)

    worker.start()
    assert store.wrote.wait(2.0)
    stop.set()
    worker.join(2.0)

    assert not worker.is_alive() and client.calls == 1

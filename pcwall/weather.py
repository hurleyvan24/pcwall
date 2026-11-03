"""Open-Meteo weather client for the pcwall display (Wave 4.1).

Standard library only: urllib.request fetches, json parses. Nothing here imports a
third-party weather package, and nothing here opens a socket unless the fetch callable is
the default urllib.request.urlopen. Tests hand OpenMeteo a fake fetch and stay offline.

The public surface:

    Location, Forecast        frozen records the wall stores and renders
    WeatherError              the one exception callers need to catch
    WMO_CONDITIONS            WMO weather code -> (emoji, label)
    condition_for(code)       the same lookup with a safe fallback for any code
    OpenMeteo                 geocoding plus forecast client with an injected fetch
    weather_loop              background worker that refreshes the store's "weather" key

Every request uses a 10 second timeout and reads at most 512 KiB. A larger reply, an HTTP
error, a network failure, unparseable JSON or an unknown city all raise WeatherError.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

__all__ = [
    "FORECAST_URL",
    "GEOCODING_URL",
    "MAX_PAYLOAD_BYTES",
    "TIMEOUT_SECONDS",
    "WMO_CONDITIONS",
    "Forecast",
    "Location",
    "OpenMeteo",
    "WeatherError",
    "condition_for",
    "weather_loop",
]

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_SECONDS = 10
MAX_PAYLOAD_BYTES = 512 * 1024
USER_AGENT = "pcwall/1.0 (family display; python urllib)"
UNITS = ("fahrenheit", "celsius")
FORECAST_DAYS = 7
FORECAST_HOURS = 24

# Open-Meteo variable names (the current API spelling; the legacy spellings are accepted
# when parsing so an older cached payload still renders).
CURRENT_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "weather_code",
    "wind_speed_10m",
    "is_day",
)
HOURLY_FIELDS = ("temperature_2m", "weather_code", "precipitation_probability")
DAILY_FIELDS = (
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
    "sunrise",
    "sunset",
)


class WeatherError(RuntimeError):
    """Every way a weather lookup can fail: HTTP errors, network failures, oversized or
    unparseable replies, an Open-Meteo error document, and a city the geocoder does not
    know."""


@dataclass(frozen=True)
class Location:
    name: str
    latitude: float
    longitude: float
    timezone: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Forecast:
    """Parsed forecast. `current` is one dict, `hourly` and `daily` are lists of dicts (one
    per hour / day) and `fetched_at` is an ISO-8601 UTC timestamp. Everything inside is
    plain JSON-friendly data so it can go straight into the wall's data document."""

    current: dict[str, Any]
    hourly: list[dict[str, Any]]
    daily: list[dict[str, Any]]
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": dict(self.current),
            "hourly": [dict(row) for row in self.hourly],
            "daily": [dict(row) for row in self.daily],
            "fetched_at": self.fetched_at,
        }


# ---------------------------------------------------------------------------------------
# WMO weather codes
# ---------------------------------------------------------------------------------------

def _spread(table: dict[int, tuple[str, str]], codes, emoji: str, label: str) -> None:
    for code in codes:
        table[code] = (emoji, label)


WMO_CONDITIONS: dict[int, tuple[str, str]] = {
    0: ("☀️", "Clear"),
    1: ("🌤️", "Mainly Clear"),
    2: ("⛅", "Partly Cloudy"),
    3: ("☁️", "Overcast"),
}
_spread(WMO_CONDITIONS, (45, 48), "🌫️", "Fog")
_spread(WMO_CONDITIONS, range(51, 56), "🌦️", "Drizzle")
_spread(WMO_CONDITIONS, (56, 57), "🌨️", "Freezing Drizzle")
_spread(WMO_CONDITIONS, range(61, 66), "🌧️", "Rain")
_spread(WMO_CONDITIONS, (66, 67), "🌨️", "Freezing Rain")
_spread(WMO_CONDITIONS, range(71, 78), "❄️", "Snow")
_spread(WMO_CONDITIONS, range(80, 83), "🌧️", "Rain Showers")
_spread(WMO_CONDITIONS, (85, 86), "🌨️", "Snow Showers")
_spread(WMO_CONDITIONS, range(95, 100), "⛈️", "Thunderstorm")

FALLBACK_CONDITION = ("🌡️", "Unknown")

# WMO groups its two-digit codes by tens (40s fog, 50s drizzle, 60s rain, 70s snow, 80s
# showers, 90s thunder). A code Open-Meteo never emits still lands in its decade's family.
_DECADE_FALLBACK: dict[int, tuple[str, str]] = {
    4: ("🌫️", "Fog"),
    5: ("🌦️", "Drizzle"),
    6: ("🌧️", "Rain"),
    7: ("❄️", "Snow"),
    8: ("🌧️", "Rain Showers"),
    9: ("⛈️", "Thunderstorm"),
}


def condition_for(wmo_code: Any) -> tuple[str, str]:
    """(emoji, label) for a WMO weather code. Never raises: a code outside 0..99, a
    non-integer or None gets the generic fallback, and an unlisted code inside 0..99 gets
    its decade's family."""
    if isinstance(wmo_code, bool):
        return FALLBACK_CONDITION
    try:
        code = int(wmo_code)
    except (TypeError, ValueError):
        return FALLBACK_CONDITION
    known = WMO_CONDITIONS.get(code)
    if known is not None:
        return known
    if 0 <= code <= 99:
        return _DECADE_FALLBACK.get(code // 10, FALLBACK_CONDITION)
    return FALLBACK_CONDITION


# ---------------------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------------------

FetchFn = Callable[..., Any]


class OpenMeteo:
    """Open-Meteo client. Give it either `lat` and `lon` (used as-is, no geocoding) or a
    `city` (resolved once through the Open-Meteo geocoding API and cached). `fetch` is
    called as fetch(request, timeout=TIMEOUT_SECONDS) with a urllib.request.Request and
    must return a response with read(n) and ideally close(); it defaults to urlopen."""

    def __init__(
        self,
        lat: float | None = None,
        lon: float | None = None,
        city: str | None = None,
        units: str = "fahrenheit",
        fetch: FetchFn = urllib.request.urlopen,
    ) -> None:
        if units not in UNITS:
            raise ValueError(f"units must be one of {', '.join(UNITS)}, not {units!r}")
        if (lat is None) != (lon is None):
            raise ValueError("lat and lon must be given together")
        city = (city or "").strip() or None
        if lat is None and city is None:
            raise ValueError("give either lat and lon or a city name")
        if lat is not None:
            lat = float(lat)
            lon = float(lon)
            if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
                raise ValueError(f"coordinates out of range: {lat}, {lon}")
        self.lat = lat
        self.lon = lon
        self.city = city
        self.units = units
        self.fetch = fetch
        self._location: Location | None = None

    # -- units ---------------------------------------------------------------------------

    @property
    def temperature_unit(self) -> str:
        return "°F" if self.units == "fahrenheit" else "°C"

    @property
    def wind_speed_unit(self) -> str:
        return "mph" if self.units == "fahrenheit" else "km/h"

    @property
    def location(self) -> Location | None:
        """The resolved location, or None until resolve_location() has succeeded."""
        return self._location

    # -- HTTP ----------------------------------------------------------------------------

    def _get_json(self, url: str) -> Any:
        """GET `url`, enforce the timeout and payload ceiling, and return the parsed JSON.
        Everything that can go wrong becomes a WeatherError."""
        host = urllib.parse.urlsplit(url).hostname or url
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            response = self.fetch(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            raise WeatherError(f"HTTP {exc.code} from {host}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise WeatherError(f"could not reach {host}: {exc.reason}") from exc
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise WeatherError(f"could not reach {host}: {exc}") from exc
        try:
            status = getattr(response, "status", None)
            if isinstance(status, int) and status >= 400:
                raise WeatherError(f"HTTP {status} from {host}")
            raw = response.read(MAX_PAYLOAD_BYTES + 1)
        except WeatherError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise WeatherError(f"reading from {host} failed: {exc}") from exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise WeatherError(f"reply from {host} exceeds {MAX_PAYLOAD_BYTES // 1024} KiB")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WeatherError(f"invalid JSON from {host}: {exc}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise WeatherError(f"{host} reported: {payload.get('reason') or 'error'}")
        return payload

    # -- geocoding -----------------------------------------------------------------------

    def geocoding_url(self) -> str:
        params = {"name": self.city, "count": 1, "language": "en", "format": "json"}
        return GEOCODING_URL + "?" + urllib.parse.urlencode(params)

    def resolve_location(self) -> Location:
        """Coordinates win and never touch the network: Location('Custom', lat, lon, 'auto').
        Otherwise the city is geocoded once and cached. Raises WeatherError when the
        geocoder returns no results."""
        if self._location is not None:
            return self._location
        if self.lat is not None and self.lon is not None:
            self._location = Location("Custom", self.lat, self.lon, "auto")
            return self._location
        payload = self._get_json(self.geocoding_url())
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            raise WeatherError(f"city {self.city!r} not found")
        hit = results[0] if isinstance(results[0], dict) else {}
        try:
            latitude = float(hit["latitude"])
            longitude = float(hit["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError(f"geocoding result for {self.city!r} has no coordinates") from exc
        name = str(hit.get("name") or self.city)
        tz = str(hit.get("timezone") or "auto")
        self._location = Location(name, latitude, longitude, tz)
        return self._location

    # -- forecast ------------------------------------------------------------------------

    def forecast_url(self, location: Location) -> str:
        imperial = self.units == "fahrenheit"
        params = {
            "latitude": f"{location.latitude:.4f}",
            "longitude": f"{location.longitude:.4f}",
            "current": ",".join(CURRENT_FIELDS),
            "hourly": ",".join(HOURLY_FIELDS),
            "daily": ",".join(DAILY_FIELDS),
            "temperature_unit": self.units,
            "wind_speed_unit": "mph" if imperial else "kmh",
            "precipitation_unit": "inch" if imperial else "mm",
            "timezone": location.timezone,
            "forecast_days": FORECAST_DAYS,
            "forecast_hours": FORECAST_HOURS,
        }
        return FORECAST_URL + "?" + urllib.parse.urlencode(params)

    def fetch_forecast(self) -> Forecast:
        """Resolve the location if needed, fetch the forecast and parse it."""
        location = self.resolve_location()
        payload = self._get_json(self.forecast_url(location))
        if not isinstance(payload, dict):
            raise WeatherError("forecast reply is not a JSON object")
        return self.parse_forecast(payload)

    def parse_forecast(self, payload: dict[str, Any], fetched_at: str | None = None) -> Forecast:
        """Turn an Open-Meteo forecast document into a Forecast. Hourly and daily blocks are
        optional (they parse to empty lists); current conditions are required."""
        current_block = payload.get("current")
        if not isinstance(current_block, dict):
            raise WeatherError("forecast reply has no current conditions")
        hourly_block = payload.get("hourly")
        daily_block = payload.get("daily")
        return Forecast(
            current=self._parse_current(current_block),
            hourly=self._parse_hourly(hourly_block if isinstance(hourly_block, dict) else {}),
            daily=self._parse_daily(daily_block if isinstance(daily_block, dict) else {}),
            fetched_at=fetched_at or _utcnow_iso(),
        )

    def _format_temperature(self, value: Any) -> str:
        return "--" if value is None else f"{round(value)}{self.temperature_unit}"

    def _parse_current(self, block: dict[str, Any]) -> dict[str, Any]:
        code = _int_or_none(_pick(block, "weather_code", "weathercode"))
        emoji, label = condition_for(code)
        temperature = _number(_pick(block, "temperature_2m", "temperature"))
        feels_like = _number(block.get("apparent_temperature"))
        humidity = _number(_pick(block, "relative_humidity_2m", "relativehumidity_2m"))
        wind = _number(_pick(block, "wind_speed_10m", "windspeed_10m", "windspeed"))
        is_day = block.get("is_day", 1)
        return {
            "time": block.get("time"),
            "code": code,
            "emoji": emoji,
            "label": label,
            "temperature": temperature,
            "temperature_text": self._format_temperature(temperature),
            "feels_like": feels_like,
            "feels_like_text": self._format_temperature(feels_like),
            "humidity": humidity,
            "humidity_text": "--" if humidity is None else f"{round(humidity)}%",
            "wind_speed": wind,
            "wind_text": "--" if wind is None else f"{round(wind)} {self.wind_speed_unit}",
            "is_day": bool(is_day) if is_day is not None else True,
            "temperature_unit": self.temperature_unit,
            "wind_speed_unit": self.wind_speed_unit,
        }

    def _parse_hourly(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        times = _column(block, "time")
        temps = _column(block, "temperature_2m")
        codes = _column(block, "weather_code", "weathercode")
        chances = _column(block, "precipitation_probability")
        rows = []
        for index, when in enumerate(times):
            code = _int_or_none(_at(codes, index))
            emoji, label = condition_for(code)
            temperature = _number(_at(temps, index))
            rows.append(
                {
                    "time": when,
                    "code": code,
                    "emoji": emoji,
                    "label": label,
                    "temperature": temperature,
                    "temperature_text": self._format_temperature(temperature),
                    "precipitation_probability": _number(_at(chances, index)),
                }
            )
        return rows

    def _parse_daily(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        dates = _column(block, "time")
        codes = _column(block, "weather_code", "weathercode")
        highs = _column(block, "temperature_2m_max")
        lows = _column(block, "temperature_2m_min")
        chances = _column(block, "precipitation_probability_max")
        sunrises = _column(block, "sunrise")
        sunsets = _column(block, "sunset")
        rows = []
        for index, date in enumerate(dates):
            code = _int_or_none(_at(codes, index))
            emoji, label = condition_for(code)
            high = _number(_at(highs, index))
            low = _number(_at(lows, index))
            rows.append(
                {
                    "date": date,
                    "weekday": _weekday(date),
                    "code": code,
                    "emoji": emoji,
                    "label": label,
                    "high": high,
                    "high_text": self._format_temperature(high),
                    "low": low,
                    "low_text": self._format_temperature(low),
                    "precipitation_probability": _number(_at(chances, index)),
                    "sunrise": _at(sunrises, index),
                    "sunset": _at(sunsets, index),
                }
            )
        return rows


# ---------------------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------------------

def weather_loop(store, client, refresh_minutes: int, stop: threading.Event) -> None:
    """Refresh store["weather"] every `refresh_minutes` until `stop` is set. The first fetch
    happens immediately. Between fetches the loop sleeps one second at a time and checks
    `stop`, so stopping never waits longer than a second. A failed fetch records the error
    next to the last good forecast instead of replacing it, and no exception escapes the
    thread."""
    seconds = max(1, int(round(float(refresh_minutes) * 60)))
    while not stop.is_set():
        try:
            forecast = client.fetch_forecast()
        except WeatherError as exc:
            _record_failure(store, str(exc))
        except Exception as exc:  # a bug in the client must not kill the worker
            _record_failure(store, f"{type(exc).__name__}: {exc}")
        else:
            _record_forecast(store, client, forecast)
        for _ in range(seconds):
            if stop.is_set():
                return
            time.sleep(1)


def _record_forecast(store, client, forecast: Forecast) -> None:
    location = getattr(client, "location", None)
    snapshot = {
        "location": location.to_dict() if isinstance(location, Location) else None,
        "units": getattr(client, "units", None),
        **forecast.to_dict(),
        "error": None,
        "error_at": None,
    }

    def change(draft, now):
        draft["weather"] = dict(snapshot, updated_at=now.isoformat())

    _transact(store, change)


def _record_failure(store, message: str) -> None:
    def change(draft, now):
        weather = draft.get("weather")
        if not isinstance(weather, dict):
            weather = {}
            draft["weather"] = weather
        weather["error"] = message
        weather["error_at"] = now.isoformat()

    _transact(store, change)


def _transact(store, change) -> None:
    try:
        store.transact(change, kinds=("weather",))
    except Exception as exc:  # the store already logged what it could; keep the worker alive
        print(f"family wall: weather update not saved: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pick(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _column(block: dict[str, Any], *keys: str) -> list:
    value = _pick(block, *keys)
    return value if isinstance(value, list) else []


def _at(values: list, index: int) -> Any:
    return values[index] if index < len(values) else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int_or_none(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _weekday(date: Any) -> str:
    try:
        return datetime.strptime(str(date), "%Y-%m-%d").strftime("%a")
    except ValueError:
        return ""

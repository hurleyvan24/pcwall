"""Focused, offline coverage for standalone Google authentication and sync."""

import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pcwall import google_sync


@pytest.fixture
def paths(tmp_path, monkeypatch):
    client = tmp_path / "client.json"
    token = tmp_path / "nested" / "token.json"
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", str(client))
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(token))
    return client, token


@pytest.fixture
def sdk(monkeypatch):
    """Replace only Google SDK boundaries; token files and wall logic stay real."""
    credentials = SimpleNamespace(valid=True, expired=False, refresh_token="refresh",
                                  refresh=Mock(), to_json=lambda: '{"token": "saved"}')
    loader = Mock(return_value=credentials)
    request = Mock(return_value=object())
    flow = SimpleNamespace(run_local_server=Mock(return_value=credentials))
    factory = Mock(return_value=flow)
    build = Mock(side_effect=lambda api, version, **kwargs: (api, version, kwargs))
    session = Mock(side_effect=lambda creds: ("session", creds))
    modules = {
        "google.oauth2.credentials": {"Credentials": SimpleNamespace(from_authorized_user_file=loader)},
        "google.auth.transport.requests": {"Request": request, "AuthorizedSession": session},
        "google_auth_oauthlib.flow": {"InstalledAppFlow": SimpleNamespace(from_client_secrets_file=factory)},
        "googleapiclient.discovery": {"build": build},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(credentials=credentials, loader=loader, request=request,
                           factory=factory, flow=flow, build=build)


def test_default_paths_are_rooted_at_repository_not_working_directory(tmp_path, monkeypatch):
    auth = importlib.import_module("pcwall.google_auth")
    monkeypatch.delenv("GOOGLE_CREDENTIALS_FILE", raising=False)
    monkeypatch.delenv("GOOGLE_TOKEN_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert auth.credentials_path() == ROOT / "credentials.json"
    assert auth.token_path() == ROOT / "data" / "google_token.json"


@pytest.mark.parametrize("absolute", [False, True])
def test_path_environment_overrides(tmp_path, monkeypatch, absolute):
    auth = importlib.import_module("pcwall.google_auth")
    base = tmp_path if absolute else Path("custom")
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", str(base / "client.json"))
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(base / "token.json"))
    monkeypatch.chdir(tmp_path)
    expected = base if absolute else ROOT / "custom"
    assert auth.credentials_path() == expected / "client.json"
    assert auth.token_path() == expected / "token.json"


@pytest.mark.parametrize("has_client", [False, True])
def test_missing_token_never_starts_noninteractive_login(paths, sdk, has_client):
    auth = importlib.import_module("pcwall.google_auth")
    if has_client:
        paths[0].write_text("{}", encoding="utf-8")
    sdk.factory.side_effect = AssertionError("Noninteractive login opened a browser")
    assert auth.get_credentials() is None


def test_valid_token_loads_without_client_secrets(paths, sdk):
    auth = importlib.import_module("pcwall.google_auth")
    paths[1].parent.mkdir()
    paths[1].write_text("{}", encoding="utf-8")
    assert auth.get_credentials() is sdk.credentials
    assert sdk.loader.call_args.args == (str(paths[1]), [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/photospicker.mediaitems.readonly",
    ])


def test_expired_token_refreshes_and_persists(paths, sdk):
    auth = importlib.import_module("pcwall.google_auth")
    paths[1].parent.mkdir()
    paths[1].write_text("{}", encoding="utf-8")
    sdk.credentials.valid, sdk.credentials.expired = False, True

    def refresh(request):
        if request is not sdk.request.return_value:
            raise AssertionError("Refresh requires the Google transport")
        sdk.credentials.valid = True

    sdk.credentials.refresh.side_effect = refresh
    assert auth.get_credentials() is sdk.credentials
    assert json.loads(paths[1].read_text(encoding="utf-8")) == {"token": "saved"}


@pytest.mark.parametrize("failure", ["malformed", "revoked", "no_refresh"])
def test_unusable_token_returns_none_noninteractively(paths, sdk, failure):
    auth = importlib.import_module("pcwall.google_auth")
    paths[1].parent.mkdir()
    paths[1].write_text("bad token", encoding="utf-8")
    sdk.credentials.valid, sdk.credentials.expired = False, True
    if failure == "malformed":
        sdk.loader.side_effect = ValueError("Malformed token")
    elif failure == "revoked":
        sdk.credentials.refresh.side_effect = RuntimeError("Revoked token")
    else:
        sdk.credentials.refresh_token = None
    sdk.factory.side_effect = AssertionError("Noninteractive login opened a browser")
    assert auth.get_credentials() is None


@pytest.mark.parametrize("failure", ["missing", "malformed", "revoked"])
def test_interactive_login_recovers_and_creates_token_directory(paths, sdk, failure):
    auth = importlib.import_module("pcwall.google_auth")
    paths[0].write_text("{}", encoding="utf-8")
    if failure != "missing":
        paths[1].parent.mkdir()
        paths[1].write_text("{}", encoding="utf-8")
        if failure == "malformed":
            sdk.loader.side_effect = ValueError("Malformed token")
        else:
            sdk.loader.return_value = SimpleNamespace(valid=False, expired=True, refresh_token="revoked",
                                                      refresh=Mock(side_effect=RuntimeError("Revoked")))
    result = auth.get_credentials(scopes=("custom.scope",), interactive=True)
    assert (result, json.loads(paths[1].read_text(encoding="utf-8"))) == (sdk.credentials, {"token": "saved"})
    assert (sdk.factory.call_args.args, sdk.flow.run_local_server.call_args.kwargs) == (
        (str(paths[0]), ["custom.scope"]), {"port": 0})


@pytest.mark.parametrize("failure", ["missing_client", "missing_sdk", "flow", "invalid", "save"])
def test_interactive_failures_return_none(paths, sdk, failure):
    auth = importlib.import_module("pcwall.google_auth")
    if failure != "missing_client":
        paths[0].write_text("{}", encoding="utf-8")
    if failure == "missing_sdk":
        sdk.factory.side_effect = ModuleNotFoundError("OAuth SDK not installed")
    elif failure == "flow":
        sdk.flow.run_local_server.side_effect = RuntimeError("Consent declined")
    elif failure == "invalid":
        sdk.credentials.valid = False
    elif failure == "save":
        paths[1].parent.write_text("Blocks the token directory", encoding="utf-8")
    assert auth.get_credentials(interactive=True) is None


@pytest.mark.parametrize("outcome, code", [("missing", 1), ("failed", 1), ("connected", 0)])
def test_cli_reports_actionable_status(paths, monkeypatch, capsys, outcome, code):
    auth = importlib.import_module("pcwall.google_auth")
    if outcome != "missing":
        paths[0].write_text("{}", encoding="utf-8")
    monkeypatch.setattr(auth, "get_credentials", lambda **kwargs: object() if outcome == "connected" else None)
    assert auth.main([]) == code
    captured = capsys.readouterr()
    assert str(paths[1] if code == 0 else paths[0]) in captured.out + captured.err


def test_google_clients_use_standalone_credentials(paths, sdk, monkeypatch):
    auth = importlib.import_module("pcwall.google_auth")
    monkeypatch.setattr(auth, "get_credentials", lambda: sdk.credentials)
    assert (google_sync._calendar_service(), google_sync._gmail_service(), google_sync._authorized_session()) == (
        ("calendar", "v3", {"credentials": sdk.credentials, "cache_discovery": False}),
        ("gmail", "v1", {"credentials": sdk.credentials, "cache_discovery": False}),
        ("session", sdk.credentials))


@pytest.mark.parametrize("disconnected", ["hub", "calendar", "token"])
def test_disconnected_calendar_preserves_cached_events(caplog, monkeypatch, disconnected):
    state = {"settings": {"calendar": {"calendars": [{"id": "primary"}], "past_days": 1, "future_days": 7}},
             "members": [], "calendar": {"events": [{"id": "cached"}], "error": None}}
    store = SimpleNamespace(current=lambda: state, now=lambda: datetime(2026, 11, 2, tzinfo=timezone.utc),
                            zone=timezone.utc,
                            transact=Mock(side_effect=AssertionError("Disconnected sync must preserve the snapshot")))
    google = google_sync.GoogleHub(calendar=lambda: None)
    if disconnected == "hub":
        google = None
    elif disconnected == "token":
        auth = importlib.import_module("pcwall.google_auth")
        monkeypatch.setattr(auth, "get_credentials", lambda: None)
        google = google_sync.GoogleHub()
    with caplog.at_level("INFO", logger="pcwall.google_sync"):
        result = google_sync.sync_calendar_events(store, google)
    assert (result["events"], bool(result.get("skipped")), state["calendar"]["events"], bool(caplog.text)) == (
        1, True, [{"id": "cached"}], True)


def test_calendar_transforms_dates_tags_and_cancelled_events():
    members = [{"id": "kid", "name": "Child", "role": "kid"}]
    calendar = {"id": "primary", "member": "family"}
    timed = {"id": "visit", "summary": "Child appointment", "location": "Clinic",
             "start": {"dateTime": "2026-11-02T09:00:00-08:00"},
             "end": {"dateTime": "2026-11-02T10:00:00-08:00"}}
    all_day = {"id": "holiday", "summary": " ", "start": {"date": "2026-11-03"}, "end": {"date": "2026-11-04"}}
    api = Mock()
    api.events.return_value.list.return_value.execute.side_effect = [
        {"items": [timed, {"id": "cancelled", "status": "cancelled"}], "nextPageToken": "next"},
        {"items": [all_day]},
    ]
    events = google_sync.fetch_events(api, [calendar], members, start=datetime(2026, 11, 1, tzinfo=timezone.utc),
                                      end=datetime(2026, 11, 5, tzinfo=timezone.utc), zone=timezone.utc)
    assert events[0] == {"id": "visit", "calendar": "primary", "title": "Child appointment", "location": "Clinic",
                         "all_day": False, "start": "2026-11-02T17:00:00+00:00",
                         "end": "2026-11-02T18:00:00+00:00", "members": ["kid"]}
    assert events[1:] == [{"id": "holiday", "calendar": "primary", "title": "Busy", "location": "", "all_day": True,
                           "start": "2026-11-03", "end": "2026-11-04", "members": ["family"]}]


@pytest.mark.parametrize("query", [None, "from:school.example subject:theme"])
def test_school_query_is_generic_and_configurable(monkeypatch, query):
    if query is None:
        monkeypatch.delenv("PCWALL_DRESS_QUERY", raising=False)
    else:
        monkeypatch.setenv("PCWALL_DRESS_QUERY", query)
    api = Mock()
    api.users.return_value.messages.return_value.list.return_value.execute.return_value = {"messages": []}
    store = SimpleNamespace(now=lambda: datetime(2026, 11, 2, tzinfo=timezone.utc), zone=timezone.utc)
    result = google_sync.sync_heritage_dress_days(store, google_sync.GoogleHub(gmail=lambda: api))
    assert (api.users.return_value.messages.return_value.list.call_args.kwargs["q"], result) == (
        query or "subject:dress OR subject:spirit", {"found": 0, "added": 0})

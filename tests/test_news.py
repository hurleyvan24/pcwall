"""Focused contracts for feed ingestion and background refresh."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from copy import deepcopy
from datetime import datetime
import hashlib
import io
import threading
from urllib.error import HTTPError, URLError

import pytest

from pcwall import news


FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIMIT = 512 * 1024


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


def test_parse_rss_2():
    headlines = news.parse_feed((FIXTURES / "feed_rss.xml").read_bytes(), "Wire")
    assert [(h.title, h.link) for h in headlines] == [
        ("City council approves new library branch", "https://wire.example.com/2026/11/02/library-branch"),
        ("Markets rally as tech & energy stocks climb", "https://wire.example.com/2026/11/02/markets-rally"),
        ('Weekend storm brings "record" rainfall to the valley', "https://wire.example.com/2026/11/01/weekend-storm"),
        ("Local bakery marks 50 years", "https://wire.example.com/2026/10/31/bakery-50-years"),
    ]
    assert headlines[0].to_dict() == {
        "title": "City council approves new library branch",
        "link": "https://wire.example.com/2026/11/02/library-branch",
        "published": "Mon, 02 Nov 2026 14:05:00 GMT",
        "source": "Wire",
        "id": hashlib.sha1(b"https://wire.example.com/2026/11/02/library-branch").hexdigest(),
    }


def test_parse_atom_1():
    headlines = news.parse_feed((FIXTURES / "feed_atom.xml").read_bytes(), "Daily")
    assert [(h.title, h.link, h.published, h.source) for h in headlines] == [
        ("City council approves new library branch", "https://daily.example.com/library", "2026-11-02T15:00:00Z", "Daily"),
        ("Schools open new science labs", "https://daily.example.com/schools", "2026-11-02T07:45:00-05:00", "Daily"),
        ("Night sky & stars delight observers", "https://daily.example.com/sky", "2026-11-01T20:00:00Z", "Daily"),
    ]


@pytest.mark.parametrize("xml", [b"<rss><broken>", b"\xff", b"", b'<?xml version="1.0" encoding="unknown"?><rss/>'])
def test_malformed_xml_returns_empty_list(xml):
    assert news.parse_feed(xml, "Broken") == []


def test_entity_decoding_and_html_strip():
    xml = (
        b'<rss><channel><item><title><![CDATA[ <b>Fish &amp; &quot;chips&quot;</b>\n today ]]></title></item>'
        b'<item><title>' + b'x' * 160 + b'</title></item>'
        b'<item><title>   </title></item></channel></rss>'
    )
    assert [h.title for h in news.parse_feed(xml, "Wire")] == ['Fish & "chips" today', "x" * 140]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/feed", "javascript:alert(1)", "example.com/feed", ""])
def test_fetch_rejects_non_http_scheme(url):
    def forbidden_fetch(*args, **kwargs):
        pytest.fail("Invalid schemes must be rejected before network access")

    with pytest.raises(news.NewsError):
        news.fetch_feed(url, fetch=forbidden_fetch)


def test_fetch_accepts_limit_and_closes_response():
    response = Response(b"x" * LIMIT)
    calls = []

    def fetch(request, timeout):
        calls.append((request.full_url, request.get_header("User-agent"), timeout))
        return response

    assert news.fetch_feed("https://example.com/feed", fetch=fetch) == b"x" * LIMIT
    assert calls == [("https://example.com/feed", "pcwall/0.1.0 (news reader)", 10)]
    assert (response.closed, response.read_sizes) == (True, [LIMIT + 1])


def test_fetch_enforces_size_cap():
    response = Response(b"x" * (LIMIT + 50))
    with pytest.raises(news.NewsError, match="Feed exceeds 512KB limit"):
        news.fetch_feed("http://example.com/feed", fetch=lambda *a, **kw: response)
    assert (response.closed, response.read_sizes) == (True, [LIMIT + 1])


def test_fetch_closes_http_error():
    body = io.BytesIO(b"service unavailable")

    def fetch(*args, **kwargs):
        raise HTTPError("https://example.com/feed", 503, "Unavailable", {}, body)

    with pytest.raises(news.NewsError):
        news.fetch_feed("https://example.com/feed", fetch=fetch)
    assert body.closed


@pytest.mark.parametrize("error", [URLError("offline"), TimeoutError("timed out")])
def test_fetch_wraps_network_errors(error):
    def fetch(*args, **kwargs):
        raise error

    with pytest.raises(news.NewsError):
        news.fetch_feed("https://example.com/feed", fetch=fetch)


def test_merge_interleaves_and_caps():
    old = news.Headline("Old", "old", "Mon, 02 Nov 2026 14:05:00 GMT", "Wire", "old")
    newest = news.Headline("Newest", "new", "2026-11-02T10:00:00-05:00", "Daily", "new")
    middle = news.Headline("Middle", "middle", "Mon, 02 Nov 2026 09:30:00 -0500", "Wire", "middle")
    undated = news.Headline("Undated", "none", "", "Wire", "none")
    invalid = news.Headline("Invalid", "bad", "not-a-date", "Daily", "bad")
    assert news.merge([[undated, old, middle], [invalid, newest]], max_items=3) == [newest, middle, old]
    assert news.merge([[newest]], max_items=0) == []


def test_merge_dedupes_by_title_slug():
    old = news.Headline("City council: new library!", "old", "2026-11-01T00:00:00Z", "Wire", "old")
    new = news.Headline("CITY COUNCIL -- NEW LIBRARY", "new", "2026-11-02T00:00:00Z", "Daily", "new")
    assert news.merge([[old], [new]]) == [new]


def test_news_loop_writes_headlines_to_store(monkeypatch):
    rss = (FIXTURES / "feed_rss.xml").read_bytes()
    atom = (FIXTURES / "feed_atom.xml").read_bytes()
    stop = threading.Event()
    cycle = 0

    class Store:
        def __init__(self):
            self.data = {"unrelated": "preserved"}
            self.snapshots = []

        def update(self, change):
            change(self.data)
            self.snapshots.append(deepcopy(self.data))
            if len(self.snapshots) == 2:
                stop.set()

    def settings_reader():
        nonlocal cycle
        cycle += 1
        return {"news": {"feeds": [
            {"title": "Wire", "url": "https://wire.example.com/rss.xml"},
            {"title": "Daily", "url": "https://daily.example.com/atom.xml"},
        ]}}

    def fetch_feed(url):
        if "wire.example.com" in url:
            if cycle > 1:
                raise news.NewsError("offline")
            return rss
        return atom if cycle == 1 else atom.replace(b"Schools open", b"Schools announce")

    # Keep a real thread and Event, but skip the polling delay in this unit test.
    monkeypatch.setattr(news, "fetch_feed", fetch_feed)
    monkeypatch.setattr(stop, "wait", lambda timeout: stop.is_set())
    store = Store()
    worker = threading.Thread(target=news.news_loop, args=(store, settings_reader, stop), daemon=True)
    worker.start()
    try:
        worker.join(timeout=3)
    finally:
        stop.set()
        worker.join(timeout=1)

    assert (len(store.snapshots), worker.is_alive()) == (2, False)
    assert [h["title"] for h in store.data["news"]["headlines"]] == [
        "City council approves new library branch",
        "Markets rally as tech & energy stocks climb",
        "Schools announce new science labs",
        'Weekend storm brings "record" rainfall to the valley',
        "Night sky & stars delight observers",
        "Local bakery marks 50 years",
    ]
    assert (store.data["unrelated"], store.data["news"]["sources"], datetime.fromisoformat(store.data["news"]["updated_at"]).tzinfo is not None) == ("preserved", ["Wire", "Daily"], True)

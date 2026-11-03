"""Standard-library feed ingestion for the family display."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from http.client import HTTPException
import html
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


MAX_BYTES = 512 * 1024
POLL_SECONDS = 15 * 60
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
DEFAULT_FEEDS = (
    {"title": "NPR", "url": "https://feeds.npr.org/1001/rss.xml"},
    {"title": "BBC", "url": "https://feeds.bbci.co.uk/news/rss.xml"},
)
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Headline:
    title: str
    link: str
    published: str
    source: str
    id: str

    def to_dict(self) -> dict:
        return asdict(self)


class NewsError(RuntimeError):
    """A feed could not be fetched safely."""


def fetch_feed(url: str, fetch=urllib.request.urlopen) -> bytes:
    """Fetch at most 512KB over HTTP(S), closing success and error responses."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Feed URL must use http or https")
        request = urllib.request.Request(
            url, headers={"User-Agent": "pcwall/0.1.0 (news reader)"}
        )
    except (TypeError, ValueError) as exc:
        raise NewsError("Feed URL must use http or https") from exc

    response = None
    try:
        response = fetch(request, timeout=10)
        payload = response.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise NewsError("Feed exceeds 512KB limit")
        return payload
    except urllib.error.HTTPError as exc:
        # HTTPError itself owns a response body even when urlopen never returns.
        exc.close()
        raise NewsError(f"Could not fetch feed: {exc}") from exc
    except (OSError, HTTPException, ValueError) as exc:
        raise NewsError(f"Could not fetch feed: {exc}") from exc
    finally:
        if response is not None:
            response.close()


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == name:
            return "".join(child.itertext()).strip()
    return ""


def parse_feed(xml_bytes: bytes, source_title: str) -> list[Headline]:
    """Read RSS 2.0 or Atom 1.0; malformed input produces no headlines."""
    try:
        root = ET.fromstring(xml_bytes)
    except (ET.ParseError, SyntaxError, LookupError, UnicodeError, ValueError):
        _LOG.warning("Ignoring malformed news feed from %s", source_title)
        return []

    items = root.findall("./channel/item") or root.findall(".//item")
    atom = not items
    if atom:
        items = root.findall(f".//{{{ATOM_NAMESPACE}}}entry") or root.findall(".//entry")

    headlines = []
    for item in items:
        raw_title = _child_text(item, "title")
        title = html.unescape(re.sub(r"<[^>]+>", "", raw_title))
        title = " ".join(title.split())[:140]
        if not title:
            continue

        link = _child_text(item, "link")
        if atom:
            links = [child for child in item if child.tag.rsplit("}", 1)[-1] == "link"]
            # An Atom self link identifies the entry document, not the article.
            links.sort(key=lambda child: child.get("rel", "alternate") != "alternate")
            for child in links:
                candidate = (child.get("href") or "".join(child.itertext())).strip()
                if candidate:
                    link = candidate
                    break
            published = _child_text(item, "updated") or _child_text(item, "published")
        else:
            published = _child_text(item, "pubDate")

        identity = link or title + source_title
        headlines.append(Headline(
            title=title,
            link=link,
            published=published,
            source=source_title,
            id=hashlib.sha1(identity.encode("utf-8")).hexdigest(),
        ))
    return headlines


def _published_key(headline: Headline) -> tuple[bool, datetime]:
    missing = (False, datetime.min.replace(tzinfo=timezone.utc))
    if not headline.published:
        return missing
    try:
        try:
            published = datetime.fromisoformat(headline.published.replace("Z", "+00:00"))
        except ValueError:
            published = parsedate_to_datetime(headline.published)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        return True, published.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return missing


def merge(headlines: list[list[Headline]], max_items: int = 30) -> list[Headline]:
    """Keep the newest copy of each normalized title, then cap the result."""
    if max_items <= 0:
        return []
    ordered = sorted(
        (headline for feed in headlines for headline in feed),
        key=_published_key,
        reverse=True,
    )
    seen = set()
    result = []
    for headline in ordered:
        slug = re.sub(r"[^a-z0-9]+", "", headline.title.lower())
        key = slug or " ".join(headline.title.lower().split())
        if key in seen:
            continue
        seen.add(key)
        result.append(headline)
        if len(result) == max_items:
            break
    return result


def _configured_feeds(settings: dict) -> list[tuple[str, str]]:
    feeds = settings.get("news", {}).get("feeds", DEFAULT_FEEDS)
    result = []
    for feed in feeds:
        if isinstance(feed, str):
            result.append((feed, feed))
        else:
            url = feed["url"]
            result.append((feed.get("title") or feed.get("name") or url, url))
    return result


def news_loop(store, settings_reader, stop: threading.Event) -> None:
    """Poll on a caller-owned background thread until stop is set.

    settings_reader returns a settings dict. store.transact supplies a document
    and timestamp; update-only stores supply a document. Publishing news preserves
    all other state. Each feed's last successful result survives transient failures.
    """
    cached: dict[str, list[Headline]] = {}
    while not stop.is_set():
        try:
            feeds = _configured_feeds(settings_reader())
            refreshed = False
            for title, url in feeds:
                if stop.is_set():
                    return
                try:
                    cached[url] = parse_feed(fetch_feed(url), title)
                    refreshed = True
                except Exception as exc:
                    _LOG.warning("News feed %s failed: %s", title, exc)

            if refreshed or not feeds:
                headlines = [h.to_dict() for h in merge([cached.get(url, []) for _, url in feeds])]
                sources = [title for title, _ in feeds]

                def update_news(draft, now=None):
                    draft["news"] = {
                        "headlines": headlines,
                        "items": headlines,
                        "updated_at": (now or datetime.now(timezone.utc)).isoformat(),
                        "sources": sources,
                    }

                if hasattr(store, "transact"):
                    store.transact(update_news, kinds=("news",))
                else:
                    store.update(update_news)
            # Discard removed feeds without discarding failed feeds still in use.
            cached = {url: cached[url] for _, url in feeds if url in cached}
        except Exception as exc:
            _LOG.warning("Could not refresh news: %s", exc)

        for _ in range(POLL_SECONDS):
            if stop.wait(1):
                return

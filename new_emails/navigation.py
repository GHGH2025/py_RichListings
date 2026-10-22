"""Sender strategies: navigation and acceptance are deterministic, extraction is AI."""
from dataclasses import dataclass
from html.parser import HTMLParser
import re
from collections import deque
from typing import Protocol
from urllib.parse import urljoin, urlsplit
from urllib.error import HTTPError


@dataclass(frozen=True)
class Page:
    label: str
    url: str
    html: str
    error: str | None = None


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.href = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.href, self.parts = attrs.get("href"), []
        elif tag == "img" and self.href:
            self.parts.append(attrs.get("alt", ""))

    def handle_data(self, data):
        if self.href:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append((" ".join(" ".join(self.parts).split()), self.href))
            self.href = None


def button_links(html, labels, base_url="", *, prefix=False):
    parser = LinkParser()
    parser.feed(html)
    wanted = {" ".join(label.casefold().split()) for label in labels}
    result, seen = [], set()
    for text, href in parser.links:
        url = urljoin(base_url, href)
        label = text.casefold()
        matches = label in wanted or (prefix and any(label.startswith(value + " ") for value in wanted))
        if matches and urlsplit(url).scheme in ("http", "https") and url not in seen:
            result.append((text, url))
            seen.add(url)
    return result


def missing_house_number(address):
    """Allow street names and redacted numbers; reject real leading house numbers.

    Numbered street names (5th Avenue) do not imply a house number.
    """
    value = (address or "").strip()
    if not value:
        return False
    first = value.split()[0].rstrip(",")
    if re.search(r"[xX*?]", first) or re.fullmatch(r"_+", first):
        return True
    if re.fullmatch(r"\d+(?:st|nd|rd|th)", first, flags=re.I):
        return True
    return not bool(re.match(r"^#?\d+(?:[A-Za-z]|(?:[-/]\d+))?(?:\s|,|$)", value))


class SenderHandler(Protocol):
    def pages(self, html, config, fetch): ...
    def accept(self, listing): ...


class ButtonPagesHandler:
    prompt_version = 1
    detail_button_labels = ("Get More Info", "View More Details", "Click to View More Details")
    max_depth = 3
    max_pages = 100

    def select_links(self, html, labels, base_url=""):
        return button_links(html, labels, base_url)

    def fetch_content(self, url, fetch):
        return fetch(url)

    def pages(self, html, config, fetch):
        """Visit the email itself and every selected detail/category link.

        A breadth-first traversal also follows detail buttons inside category
        pages, with cycle detection and explicit bounds. Bounds raise instead
        of silently losing deals. Each yielded page may contain many listings.
        """
        yield Page("email", "", html)
        links = self.select_links(html, config.button_labels)
        pending = deque((label, url, 1) for label, url in links)
        visited = set()
        fetched_count = 0
        max_depth = getattr(config, "max_depth", 3)
        max_pages = getattr(config, "max_pages", 100)
        detail_labels = getattr(config, "detail_button_labels", ["Get More Info", "View More Details", "Click to View More Details"])
        while pending:
            label, url, depth = pending.popleft()
            if url in visited:
                continue
            if fetched_count >= max_pages or depth > max_depth:
                raise ValueError("Navigation limit reached; increase template limits to capture all deals")
            visited.add(url)
            fetched_count += 1
            try:
                page_html = self.fetch_content(url, fetch)
            except HTTPError as exc:
                if exc.code not in (404, 410):
                    raise
                yield Page(label, url, "", error=f"HTTP {exc.code}: detail page unavailable; retained email card")
                continue
            # Relative links must resolve against the final page after tracking redirects.
            page_url = getattr(page_html, "url", url)
            visited.add(page_url)
            yield Page(label, page_url, page_html)
            pending.extend((text, href, depth + 1) for text, href in
                           self.select_links(page_html, detail_labels, page_url) if href not in visited)

    def accept(self, listing):
        return bool(listing.get("address") or listing.get("source_title"))

    def enrich_listing(self, listing, page):
        """Optional deterministic facts/media supplied by a sender's page layout."""
        return listing


    def default_config(self):
        return {"sender_email": self.sender_email, "handler_key": self.handler_key,
                "prompt": self.prompt, "prompt_version": self.prompt_version,
                "button_labels": list(self.button_labels),
                "detail_button_labels": list(self.detail_button_labels),
                "max_depth": self.max_depth, "max_pages": self.max_pages}

    def validate_config(self, values):
        """Sender-specific configuration constraints; shared API calls this hook."""
        return None

    def reconcile_key(self, listing, page, existing, default_key):
        """Optional sender hook for linking incomplete email cards to detail pages."""
        return default_key

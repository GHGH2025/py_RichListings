"""Replaceable page transport and sender-specific structured AI extraction."""
import ipaddress
import json
import os
import socket
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlsplit, urljoin


class FetchedHTML(str):
    def __new__(cls, content, url):
        value = super().__new__(cls, content)
        value.url = url
        return value


def validate_public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected a public HTTP(S) URL")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Nonstandard URL port")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Page URL must resolve to public addresses")


class PublicRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page(url):
    validate_public_url(url)
    # Do not send these requests through environment-configured private proxies.
    from urllib.request import ProxyHandler
    opener = build_opener(ProxyHandler({}), PublicRedirectHandler())
    with opener.open(Request(url, headers={"User-Agent": "RichListings/1.0"}), timeout=45) as response:
        mime = response.headers.get_content_type()
        if mime not in ("text/html", "text/plain", "application/xhtml+xml"):
            raise ValueError(f"Unsupported category page type: {mime}")
        content = response.read(2_000_001)
        if len(content) > 2_000_000:
            raise ValueError("Category page exceeds 2MB")
        return FetchedHTML(content.decode(response.headers.get_content_charset() or "utf-8", errors="replace"),
                           response.geturl())


class AIExtractor:
    def __init__(self, client=None):
        self.client = client

    def extract(self, page, config):
        from bs4 import BeautifulSoup
        from .schema import response_format
        if self.client is None:
            from openai import OpenAI
            self.client = OpenAI(timeout=180, max_retries=1)
        soup = BeautifulSoup(page.html, "html.parser")
        for element in soup(["script", "style", "noscript"]):
            element.decompose()
        for element in soup.find_all(["a", "img"]):
            for attribute in ("href", "src"):
                if element.get(attribute) and page.url:
                    element[attribute] = urljoin(page.url, element[attribute])
        content = str(soup)
        if len(content) > 180_000:
            raise ValueError("Category page too large for extraction; add a paginated handler")
        reply = self.client.chat.completions.create(
            model=os.getenv("NEW_EMAILS_OPENAI_MODEL", os.getenv("OPENAI_MODEL", "gpt-6-luna")),
            messages=[{"role": "system", "content": config.prompt},
                      {"role": "user", "content": f"Category: {page.label}\nURL: {page.url}\n{content}"}],
            response_format=response_format())
        choice = reply.choices[0]
        if choice.finish_reason != "stop" or getattr(choice.message, "refusal", None):
            raise ValueError("Incomplete or refused AI extraction")
        data = json.loads(choice.message.content)
        if not isinstance(data.get("listings"), list):
            raise ValueError("AI response did not contain listings")
        return data["listings"]

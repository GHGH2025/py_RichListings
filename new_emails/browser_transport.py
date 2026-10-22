"""Headless rendering adapter for sender pages whose deals load via JavaScript."""
import os
from urllib.error import HTTPError
from .extraction import FetchedHTML, validate_public_url


def is_javascript_shell(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    requires_js = "enable javascript to run this app" in soup.get_text(" ", strip=True).casefold()
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = soup.get_text(" ", strip=True)
    return (requires_js and len(text) < 300) or (
        len(text) < 100 and soup.find(id="root") is not None)


def fetch_rendered_page(url, *, require_price=True, prepare_page=None):
    from playwright.sync_api import sync_playwright
    validate_public_url(url)
    with sync_playwright() as playwright:
        kwargs = {"headless": True}
        channel = os.getenv("NEW_EMAILS_BROWSER_CHANNEL", "").strip()
        if channel:
            kwargs["channel"] = channel
        browser = playwright.chromium.launch(**kwargs)
        try:
            context = browser.new_context(service_workers="block")
            def route_request(route):
                try:
                    validate_public_url(route.request.url)
                except (ValueError, OSError):
                    route.abort()
                    return
                route.continue_()
            context.route("**/*", route_request)
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if response and response.status >= 400:
                raise HTTPError(page.url, response.status, "Rendered property page unavailable", None, None)
            # Wait for actual listing content, not only the SPA's mounting node.
            page.wait_for_function("""() => {
                const text = document.body?.innerText || '';
                return text.length > 300 && !text.includes('You need to enable JavaScript to run this app.');
            }""", timeout=60000)
            # Listing cards/details can mount in multiple passes after the shell.
            if require_price:
                page.wait_for_function("""() => {
                    const text = document.body?.innerText || '';
                    return /\\$[\\d,]+|(?:asking|purchase)\\s*price|property.*(?:unavailable|not available|no longer available|not found)|page not found/i.test(text);
                }""", timeout=60000)
            if prepare_page is not None:
                prepare_page(page)
            content = page.content()
            if len(content.encode("utf-8")) > 2_000_000:
                raise ValueError("Rendered property page exceeds 2MB")
            validate_public_url(page.url)
            return FetchedHTML(content, page.url)
        finally:
            browser.close()

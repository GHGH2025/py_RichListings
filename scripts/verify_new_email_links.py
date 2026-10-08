"""Read-only check of every detail link in a supplied .eml; no AI or publication."""
from email import policy
from email.parser import BytesParser
from pathlib import Path
import sys
import argparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from new_emails.handlers import button_links, registry
from new_emails.extraction import fetch_page

def main():
    from bs4 import BeautifulSoup
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("eml")
    parser.add_argument("--button-label", action="append", help="Repeat for multiple labels; defaults to Get More Info")
    parser.add_argument("--prefix", action="store_true", help="Match address-suffixed button labels")
    parser.add_argument("--handler", help="Use a sender's rendering adapter, e.g. john_v1")
    args = parser.parse_args()
    message = BytesParser(policy=policy.default).parsebytes(Path(args.eml).read_bytes())
    html = message.get_body(preferencelist=("html",)).get_content()
    links = button_links(html, args.button_label or ["Get More Info"], prefix=args.prefix)
    print(f"Found {len(links)} detail buttons")
    for index, (_, url) in enumerate(links, 1):
        try:
            html = registry.get(args.handler).fetch_content(url, fetch_page) if args.handler else fetch_page(url)
            page = BeautifulSoup(html, "html.parser")
            for node in page(["script", "style"]):
                node.decompose()
            title = page.title.get_text(" ", strip=True) if page.title else ""
            text = page.get_text(" ", strip=True)
            print(f"{index}: {title!r} | {len(text)} text characters | {text[:280]}")
        except Exception as exc:
            print(f"{index}: {type(exc).__name__}: {exc}")

if __name__ == "__main__":
    main()

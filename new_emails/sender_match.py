"""Match direct senders or explicit forwarded From headers, never arbitrary mentions."""
from email.utils import parseaddr
from html import unescape
import re


def matches_sender(from_header, text, html, sender):
    if parseaddr(from_header or "")[1].strip().casefold() == sender.casefold():
        return True
    from bs4 import BeautifulSoup
    body = text or BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    # Gmail and Outlook forwarding headers; stop at the next header.
    for match in re.finditer(r"\bFrom:\s*((?:(?!\b(?:Date|Sent|Subject|To):).){0,300})", unescape(body), re.I | re.S):
        header = match.group(1)
        emails = re.findall(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}", header)
        if emails and emails[0].casefold() == sender.casefold():
            return True
    return False

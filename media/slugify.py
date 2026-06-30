from __future__ import annotations

import re


def slugify_for_folder(name: str, fallback: str) -> str:
    """
    Create a Dropbox-safe folder slug from an address.
    - strip weird chars
    - remove spaces
    - cap length
    """
    s = (name or "").strip()
    if not s:
        s = fallback
    s = re.sub(r"[^A-Za-z0-9 ._-]", "_", s)
    s = s.replace(" ", "")
    s = s.strip("._-") or fallback
    return s[:80]

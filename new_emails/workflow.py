"""Pure sender workflow with injectable navigation transport and AI extractor."""
import re
from .registry import registry
from .extraction import AIExtractor, fetch_page


def normalized(value):
    value = str(value or "").strip().casefold()
    value = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", value)
    value = re.sub(r"[^\w\s#*-]", "", value)
    replacements = {"street": "st", "avenue": "ave", "terrace": "ter", "drive": "dr",
                    "boulevard": "blvd", "road": "rd", "north": "n", "south": "s",
                    "east": "e", "west": "w"}
    return " ".join(replacements.get(word, word) for word in value.split())


def identity(row):
    address = normalized(row.get("address"))
    if address:
        key = (address, normalized(row.get("city")), normalized(row.get("state")))
        # Street-only listings may refer to different properties on the same street.
        if not re.match(r"^\d+\s", address):
            key += tuple(str(row.get(field) or "") for field in ("bedrooms", "living_area_sqft", "unit_count"))
        return key
    return (normalized(row.get("source_title")), row.get("listing_url"))


class ExtractionResult(list):
    def __init__(self, listings, navigation_errors):
        super().__init__(listings)
        self.navigation_errors = navigation_errors


def extract_properties(html, config, *, fetch=fetch_page, extractor=None, handler_registry=registry):
    handler = handler_registry.get(config.handler_key)
    extractor = extractor or AIExtractor()
    rows, navigation_errors = {}, []
    for page in handler.pages(html, config, fetch):
        if page.error:
            navigation_errors.append({"url": page.url, "error": page.error})
            continue
        for raw in extractor.extract(page, config):
            if not handler.accept(raw):
                continue
            row = handler.enrich_listing(dict(raw), page)
            key = handler.reconcile_key(row, page, rows, identity(row))
            existing = rows.get(key)
            if existing is None:
                row["new_email_sources"] = [page.url or "email"]
                rows[key] = row
            else:
                # Detail pages enrich the email; nulls never erase known facts.
                sources = existing["new_email_sources"]
                for field, value in row.items():
                    if value is not None and value != "" and value != []:
                        if field == "images":
                            existing[field] = list(dict.fromkeys((existing.get(field) or []) + value))
                        else:
                            existing[field] = value
                if page.url and page.url not in sources:
                    sources.append(page.url)
                row = existing
            row["new_email_category"] = page.label
            if page.url:
                row["new_email_source_url"] = page.url
    if not rows and navigation_errors:
        raise ValueError("Detail pages are unavailable and no email deals could be extracted")
    return ExtractionResult(rows.values(), navigation_errors)

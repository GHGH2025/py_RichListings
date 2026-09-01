"""Central gate for external publication of web-scraped listings."""

from mongoengine.queryset.visitor import Q


def publication_enabled(listing) -> bool:
    """Email/WhatsApp records stay enabled; web records are opt-in."""
    source = getattr(listing, "input_source", None)
    return source != "web" or bool(getattr(listing, "web_publish_enabled", False))


def apply_publication_gate(queryset):
    """Filter a ParsedListing queryset to records allowed to publish."""
    return queryset.filter(Q(input_source__ne="web") | Q(web_publish_enabled=True))

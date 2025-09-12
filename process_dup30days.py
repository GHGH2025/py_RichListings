# process_dupes_30d.py
from datetime import datetime, timedelta
from typing import Optional, Tuple
from mongoengine.queryset.visitor import Q

# from mongo_engine_conn import init_db
from models import ParsedListing

NEXT_STATUS_ON_PASS = "processed"                 # what to set on pass
HISTORICAL_STATUSES = ("skipped", "posted", "ready_to_post", "passed","processed")
PRICE_DROP_THRESHOLD = 0.06                       # 6%


def _now() -> datetime:
    return datetime.utcnow()


def _best_addr_city_zip(pl: ParsedListing) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    ONLY use fields supported by your schema.
    Top-level first, then fallback to complete_info.*.
    """
    ci = pl.complete_info or {}
    addr = getattr(pl, "address", None) or ci.get("address")
    city = getattr(pl, "city", None) or ci.get("city")
    zip_ = getattr(pl, "zip", None) or ci.get("zip")

    def tidy(s): return s.strip() if isinstance(s, str) else None
    return tidy(addr), tidy(city), tidy(zip_)


def _price(pl: ParsedListing) -> Optional[float]:
    """Prefer top-level price; fallback to complete_info.list_price_usd."""
    if getattr(pl, "price", None) is not None:
        try:
            return float(pl.price)
        except Exception:
            pass
    ci = pl.complete_info or {}
    if ci.get("list_price_usd") is not None:
        try:
            return float(ci["list_price_usd"])
        except Exception:
            return None
    return None


def _reason(prefix: str, extra: str) -> str:
    return f"[dup-30d] {prefix}: {extra}"


def _find_recent_prior(addr: str, city: Optional[str], zip_: Optional[str],
                       since: datetime, exclude_id) -> Optional[ParsedListing]:
    """
    Find most-recent prior in last 30 days with SAME address (and city/zip when present),
    across top-level vs complete_info fields. Excludes the current doc.
    """
    # address match (no address_line anywhere)
    addr_q = Q(address__iexact=addr) | Q(complete_info__address__iexact=addr)

    loc_q = Q()
    if city:
        loc_q &= (Q(city__iexact=city) | Q(complete_info__city__iexact=city))
    if zip_:
        loc_q &= (Q(zip__iexact=zip_) | Q(complete_info__zip__iexact=zip_))

    qs = (
        ParsedListing.objects(
            addr_q
            & loc_q
            & Q(status__in=HISTORICAL_STATUSES)
            & Q(skipped_or_posted_at__gte=since)
            & Q(id__ne=exclude_id)
        )
        .only("price", "complete_info.list_price_usd", "skipped_or_posted_at", "status")
        .order_by("-skipped_or_posted_at")
    )
    print("addr>>",addr,">>",qs.first())
    return qs.first()


def process_not_processed_with_duplicate_rule(limit: int = 500) -> dict:
    """
    For each `not_processed` listing:
      - If NO prior (same address/city/zip) within 30d => status -> processed (clear reason)
      - If prior exists:
          * If current price is >= 6% lower than prior => processed
          * Else => skipped with rules_ai_reason explaining why
    """

    since = _now() - timedelta(days=30)
    checked = processed = skipped = missing_addr = 0

    candidates = (
        ParsedListing.objects(status="not_processed")
        .only("address", "city", "zip", "price", "complete_info", "skipped_or_posted_at")
        .limit(limit)
    )

    for pl in candidates:

        checked += 1

        addr, city, zip_ = _best_addr_city_zip(pl)
        if not addr:
            # No address => cannot dedupe reliably; conservative skip
            pl.update(
                set__status="skipped",
                set__rules_ai_reason=_reason("no address available to match", "cannot dedupe"),
                set__skipped_or_posted_at=_now(),
                set__updated_at=_now(),
            )
            skipped += 1
            missing_addr += 1
            continue

        prior = _find_recent_prior(addr, city, zip_, since, pl.id)

        if not prior:
            # No recent duplicate -> pass
            pl.update(
                set__status=NEXT_STATUS_ON_PASS,
                set__rules_ai_reason=None,
                set__updated_at=_now(),
            )
            processed += 1
            continue

        prev_price = _price(prior)
        curr_price = _price(pl)

        if prev_price is None or curr_price is None or prev_price <= 0:
            pl.update(
                set__status="skipped",
                set__rules_ai_reason=_reason(
                    "duplicate found but price comparison unavailable",
                    f"prev={prev_price}, curr={curr_price}"
                ),
                set__skipped_or_posted_at=_now(),
                set__updated_at=_now(),
            )
            skipped += 1
            continue

        drop = (prev_price - curr_price) / prev_price
        if drop >= PRICE_DROP_THRESHOLD:
            pl.update(
                set__status=NEXT_STATUS_ON_PASS,
                set__rules_ai_reason=None,
                set__updated_at=_now(),
            )
            processed += 1
        else:
            pl.update(
                set__status="skipped",
                set__rules_ai_reason=_reason(
                    "duplicate found; price not low enough",
                    f"drop={drop:.1%} (< 6%) prev={prev_price:.0f} -> curr={curr_price:.0f}"
                ),
                set__skipped_or_posted_at=_now(),
                set__updated_at=_now(),
            )
            skipped += 1

    return {
        "checked": checked,
        "processed": processed,
        "skipped": skipped,
        "missing_address": missing_addr,
        "lookback_days": 30,
        "price_drop_threshold": PRICE_DROP_THRESHOLD,
        "next_status_on_pass": NEXT_STATUS_ON_PASS,
    }


# if __name__ == "__main__":
#     stats = process_not_processed_with_duplicate_rule(limit=500)
#     print(stats)

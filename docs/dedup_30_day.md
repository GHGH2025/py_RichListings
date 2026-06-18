# 30-Day Dedup Rule

This document describes **Stage B** of the qualification pipeline: duplicate detection that prevents reposting the same property within 30 days unless the price has dropped by at least 6%.

For the full pipeline context, see [architecture.md](./architecture.md).

---

## Purpose

Wholesaler blast emails often repeat the same property. The 30-day dedup rule:

- **Blocks** reposts of properties already seen in the last 30 days
- **Allows** reposts when the price has dropped **≥ 6%** (treated as a meaningful price-drop update)

This runs after media verification and before AI business rules.

---

## File and entry point

| Item | Value |
|------|-------|
| **Module** | `process_dup30days.py` |
| **Function** | `process_not_processed_with_duplicate_rule()` |
| **Scheduler** | `server_runner.py` → `run_process_dup30days` |
| **Interval** | Every **1 minute** |
| **Batch size** | Up to **500** listings per run |

---

## Status transition

```
verified  →  processed   (pass — no duplicate, or ≥ 6% price drop)
verified  →  skipped     (duplicate with insufficient price drop)
```

On pass, `rules_ai_reason` is cleared. On skip, a `[dup-30d] ...` reason is stored.

---

## Constants

| Constant | Value | Meaning |
|----------|-------|---------|
| `PRICE_DROP_THRESHOLD` | `0.06` (6%) | Minimum price reduction to allow repost |
| Lookback window | **30 days** | From `skipped_or_posted_at` on prior listings |
| `NEXT_STATUS_ON_PASS` | `"processed"` | Status when listing passes dedup |

---

## Prior listings considered “historical”

A prior listing counts as a duplicate candidate only if:

1. Its `status` is one of:
   - `skipped`, `posted`, `ready_to_post`, `passed`, `processed`
   - `ready_for_image_processing`, `ready_for_primary_image_check`
2. Its `skipped_or_posted_at` is **within the last 30 days** (UTC)
3. It is not the current listing (`id != current`)

The most recent matching prior (by `skipped_or_posted_at` descending) is used for comparison.

---

## Matching strategy

Matching runs in two phases. Phase 1 must fail before phase 2 is tried.

### Phase 1 — Address match

Build up to two address candidates from the current listing:

1. Top-level `address`, `city`, `zip`
2. Raw `complete_info.address`, `complete_info.city`, `complete_info.zip` (if different)

For each candidate, search for a prior listing where:

- Address matches case-insensitively on **either** top-level or `complete_info.address`
- City matches (when present) on top-level or `complete_info.city`
- Zip matches (when present) on top-level or `complete_info.zip`

### Phase 2 — Geo fallback

If address match finds nothing, use `geo_code_response` (Google Geocoding), fetching/geocoding if missing:

| Priority | Match type |
|----------|------------|
| 1 | Exact `place_id` |
| 2 | Exact `formatted_address` (case-insensitive) |
| 3 | `formatted_address` contains street number + route + postal code |

**Masked addresses:** If the address starts with a masked run (e.g. `2*** SW Natura Ave`), the system normalizes `***`, `___`, `---` to `xxx` before geocoding.

Geo partial match (priority 3) requires **street number + route + postal** together to avoid false positives on the same street.

---

## Decision tree

```
For each listing with status = "verified":

  Has usable address (formatted or complete_info)?
    NO  → skip (reason: no address available to match)

  Prior listing found within 30 days?
    NO  → processed (pass)

  Can compare prices (both valid, prior > 0)?
    NO  → skip (reason: price comparison unavailable)

  Price drop = (prev - curr) / prev
    drop ≥ 6%  → processed (pass — price-drop repost allowed)
    drop < 6%  → skip (reason: duplicate found; price not low enough)
```

### Price sources

Price is read from:

1. Top-level `price` field
2. Fallback: `complete_info.list_price_usd`

---

## Skip reason examples

Stored in `rules_ai_reason`:

```
[dup-30d] no address available to match: cannot dedupe
[dup-30d] duplicate found but price comparison unavailable: prev_id=... prev=..., curr=...
[dup-30d] duplicate found; price not low enough: prev_id=... drop=2.3% (< 6%) prev=450000 -> curr=440000
```

`skipped_or_posted_at` is set when a listing is skipped.

---

## Return value (job stats)

```python
{
    "checked": int,
    "processed": int,
    "skipped": int,
    "missing_address": int,
    "lookback_days": 30,
    "price_drop_threshold": 0.06,
    "next_status_on_pass": "processed"
}
```

---

## Pipeline position

```
not_processed
    ↓  (media verify)
verified
    ↓  ← YOU ARE HERE (30-day dedup)
processed ──→ skipped
    ↓  (AI rules — ai_nl_rules_runner)
passed / skipped
```

See also:

- [Media verification](./media_verify.md) — previous stage
- [Post selection](./post_selection.md) — runs after AI rules and post-selection policies

---

## Design notes

- **Conservative on missing address:** Listings without any address candidate are skipped rather than passed through
- **6% threshold:** Allows legitimate price-drop updates to be reposted; same or slightly lower prices are suppressed
- **Geo fallback:** Catches duplicates when address formatting differs between emails but Google resolves to the same place
- **Historical statuses include `skipped`:** A property skipped for any reason still counts as “seen” for dedup purposes

---

## Common issues

| Symptom | Likely cause | What to check |
|---------|--------------|---------------|
| Legitimate new listing skipped as duplicate | Address/geo matches a different unit or prior bad geocode | `geo_code_response`, address fields, prior listing `skipped_or_posted_at` |
| Duplicate reposted when it should not | Price dropped ≥ 6% vs prior | Prior price, current price, `PRICE_DROP_THRESHOLD` |
| Listing skipped with price unavailable | Missing or zero price on current or prior | `price`, `complete_info.list_price_usd` |
| Dedup not catching obvious duplicate | Address text differs, geo not populated | `geo_code_response`, masked address normalization |

---

## Key files

```
py_RichListings/
├── process_dup30days.py     # This stage
├── google_formatter.py      # geocode_response() for geo fallback
├── server_runner.py         # Scheduler (every 1 min)
└── models/__init__.py       # ParsedListing schema
```

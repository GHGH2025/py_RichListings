# Post Selection

This document describes **Stage D** of the qualification pipeline: which listings that passed AI rules are allowed to proceed toward posting, including region filters, do-not-post cities, daily quotas, and Dropbox gallery upload.

For the full pipeline context, see [architecture.md](./architecture.md).

---

## Purpose

After AI business rules mark a listing as `passed`, post selection applies **business posting policies**:

1. Block listings in configured do-not-post cities
2. Block listings outside allowed Florida regions
3. Cap how many `rest_of_florida` listings can proceed relative to core South Florida volume (35% daily policy)
4. Upload external gallery links to Dropbox for use in WhatsApp ads and WordPress

Listings that survive advance to image curation.

---

## File and entry point

| Item | Value |
|------|-------|
| **Module** | `post_selection.py` |
| **Function** | `select_passed_listings_for_post()` |
| **Scheduler** | `server_runner.py` → `run_select_passed_listings_for_post` |
| **Interval** | Every **10 minutes** |
| **Batch size** | Up to **200** listings per run (oldest `created_at` first) |

---

## Status transitions

```
passed  →  ready_for_image_processing   (kept)
passed  →  skipped                      (do-not-post city or bad region)
passed  →  skipped_quota                (rest_of_florida daily cap exceeded)
```

---

## Processing order

Policies are applied in this sequence:

```
1. Do-not-post city check (AI)
       ↓ skip → status: skipped
2. Region bucket check
       ↓ bad region → status: skipped
3. 35% rest_of_florida cap
       ↓ overflow → status: skipped_quota
4. Kept listings → ready_for_image_processing + optional Dropbox upload
```

Listings skipped in step 1 are **excluded** from region and quota calculations.

---

## Policy 1 — Do-not-post cities

**Config file:** `do_not_post_city.json` (array of city names)

**Matching:**

1. Fast exact match (case-insensitive) against the JSON list
2. Normalization for `St.` vs `Saint`, dots, spacing
3. **AI fallback** (`gpt-4o-mini`) for fuzzy match — abbreviations, minor typos, extra neighborhood text

AI results are cached per city string. If AI fails, the listing is **not** blocked (fail-open).

**On skip:**

| Field | Value |
|-------|-------|
| `status` | `skipped` |
| `rules_ai_rule_id` | `Do_Not_Post_City` |
| `rules_ai_reason` | `Skipped due to Do Not Post City rule` |
| `do_not_post_city` | `found` |
| Webhook | `SKIPPED_LISTING_WEBHOOK_URL` with `skip_type: "Do_Not_Post_City"` |

---

## Policy 2 — Allowed regions

Region comes from `complete_info.region_bucket`.

**Allowed values:**

| Region bucket | Treatment |
|---------------|-----------|
| `south_florida_tri_county` | Always kept (non-rest) |
| `st_lucie` | Always kept (non-rest) |
| `fort_pierce` | Always kept (non-rest) |
| `rest_of_florida` | Subject to 35% cap |
| Anything else or missing | Skipped |

**On skip (bad region):**

| Field | Value |
|-------|-------|
| `status` | `skipped` |
| `rules_ai_rule_id` | `POST_POLICY_REGION` |
| `rules_ai_reason` | `unsupported_region_for_posting` |

---

## Policy 3 — 35% rest_of_florida daily cap

Core South Florida listings (`non_rest`) form the **base**. Rest-of-Florida listings are capped at **35%** of the cumulative daily base count.

### Formula

```
rest_cap = floor(0.35 × final_base_count)
```

Where `final_base_count` comes from the `DailyBaseCount` MongoDB collection:

- Each run adds the current batch's `non_rest` count to today's running total
- UTC day boundary determines the daily bucket
- Rest listings are sorted by queue order (oldest first); only the first `rest_cap` are kept

**Example:** If today's cumulative non-rest count is 20, then `rest_cap = floor(0.35 × 20) = 7`. At most 7 `rest_of_florida` listings from the current batch can proceed (subject to how many are already ahead in queue from earlier runs the same day).

**On skip (quota):**

| Field | Value |
|-------|-------|
| `status` | `skipped_quota` |
| `rules_ai_rule_id` | `POST_POLICY_35PC` |
| `rules_ai_reason` | `rest_of_florida_cap_exceeded_35_percent_policy: allowed=N, base_non_rest=M` |
| `over_35_percent` | `found` |
| Webhook | `SKIPPED_LISTING_WEBHOOK_URL` with `skip_type: "POST_POLICY_35PC"` |

**Note:** Quota skips use `skipped_quota`, not `skipped`. This distinguishes policy overflow from hard rule skips.

---

## Policy 4 — Dropbox gallery upload

For each **kept** listing:

- If `other_images_source` is set and `other_images_dropbox_link` is empty:
  - Create a folder under `/PropertyListings/<address-slug>/` via `dropboxImageUpload.handle_Link()`
  - Store the shared folder link in `other_images_dropbox_link`

The address slug is derived from the listing address (sanitized, max 80 chars). Dropbox failures are logged but **do not block** the listing from advancing.

The Dropbox link is later included in WhatsApp ad copy (see [WhatsApp ad generation](./whatsapp_ad_generation.md)).

---

## Fields read and written

### Read

| Field | Purpose |
|-------|---------|
| `status` | Must be `passed` |
| `complete_info.region_bucket` | Region filter |
| `city` / `complete_info.city` | Do-not-post check |
| `other_images_source` | Dropbox upload source |
| `other_images_dropbox_link` | Skip upload if already set |
| `address` | Dropbox folder name |
| `created_at` | Queue ordering |

### Written (kept listings)

| Field | Value |
|-------|-------|
| `status` | `ready_for_image_processing` |
| `do_not_post_city` | `not_found` |
| `over_35_percent` | `not_found` |
| `other_images_dropbox_link` | Set when Dropbox upload succeeds |
| `updated_at` | UTC timestamp |

---

## Return value (job stats)

```python
{
    "total_candidates": int,
    "non_rest_count": int,
    "rest_count": int,
    "rest_cap": int,
    "kept_count": int,
    "kept_ids": [...],
    "skipped_count": int,
    "skipped_ids": [...]
}
```

---

## Pipeline position

```
processed
    ↓  (AI rules)
passed
    ↓  ← YOU ARE HERE (post selection)
ready_for_image_processing ──→ skipped / skipped_quota
    ↓  (image curation — see image_curation.md)
ready_for_primary_image_check / ready_to_post
```

See also:

- [Image curation](./image_curation.md) — next stage
- [30-day dedup](./dedup_30_day.md) — earlier duplicate filter (different purpose)

---

## Webhooks

When `SKIPPED_LISTING_WEBHOOK_URL` is set, skipped listings send a JSON payload including:

- `listing_id`, `skip_type`, `reason`, full listing document
- For quota skips: `extra.rest_cap`, `extra.final_base_count`

Used for Podio Flow automations and external tracking.

---

## Common issues

| Symptom | Likely cause | What to check |
|---------|--------------|---------------|
| Good listing `skipped_quota` | Too many `rest_of_florida` vs non-rest today | `DailyBaseCount`, region_bucket, batch order |
| Listing skipped for city not on list | AI fuzzy match false positive | `do_not_post_city.json`, `rules_ai_reason`, `_AI_CITY_CACHE` (restart clears) |
| Missing Dropbox link in WhatsApp post | Upload failed or no `other_images_source` | Logs for `dropbox_upload_error`, `other_images_source` |
| Listing skipped for region | `region_bucket` missing or wrong | `complete_info.region_bucket` from extraction |

---

## Key files

```
py_RichListings/
├── post_selection.py          # This stage
├── do_not_post_city.json      # Blocked cities list
├── dropboxImageUpload.py      # Gallery folder upload
├── server_runner.py           # Scheduler (every 10 min)
└── models/__init__.py         # ParsedListing, DailyBaseCount
```

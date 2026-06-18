# Image Curation

This document describes **Stage E** of the qualification pipeline: AI-powered filtering, ordering, and validation of property photos before a listing is ready for WhatsApp ad generation.

For the full pipeline context, see [architecture.md](./architecture.md).

---

## Purpose

Wholesaler emails often include logos, flyers, agent headshots, maps, and other non-property images mixed with real photos. Image curation:

1. **Filters** invalid images using OpenAI vision (per-image classification)
2. **Orders** kept images with the best cover photo first
3. **Verifies** the primary image (`images[0]`) with a dual-model consensus check

This ensures WhatsApp and WordPress receive clean, property-relevant photos.

---

## Module and jobs

| Job | Function | Input status | Output status |
|-----|----------|--------------|---------------|
| **Curation** | `process_listings_ready_for_image_processing()` | `ready_for_image_processing` | `ready_for_primary_image_check` or `ready_to_post` |
| **Primary check** | `process_primary_image_verification()` | `ready_for_primary_image_check` | `ready_to_post` or `primary_image_failed` |

Both jobs are scheduled from `server_runner.py` every **2 minutes**, processing up to **5** listings each.

**Vision model:** `OPENAI_VISION_MODEL` env var (default `gpt-4.1-mini`) for curation; primary check uses `gpt-5.1` + `gpt-5-mini`.

---

## Stage 1 — Image curation

### Flow

```
ready_for_image_processing
    │
    ├─ images empty?
    │     YES → ready_to_post (skip curation and primary check)
    │
    └─ images present?
          → classify each URL (keep / skip)
          → order kept images (best cover first)
          → save images + skipped_images
          → ready_for_primary_image_check
```

### Per-image classification

Each URL is sent individually to the vision model with `CURATOR_CLASSIFIER_PROMPT`.

**Keep (`keep: true`) — valid property photos:**

- Building exterior or interior
- Land / parcel / vacant lot (ground or aerial)
- Driveways, garages, parking associated with the property
- Small watermark overlays on otherwise valid photos are OK

**Skip (`keep: false`) — examples:**

- Logos, QR codes, headshots, agent cards, marketing banners
- Text tiles, flyers, price graphics
- Maps, floor plans, documents, app/website screenshots
- Unrelated stock photos or other properties

Skipped images are stored in `skipped_images` as:

```json
{ "url": "...", "reason": "logo" }
```

Vision API errors also skip the image with reason `vision_error: ...`.

### Ordering

After filtering, kept images are passed to `order_property_images()` with `CURATOR_ORDERING_PROMPT`:

1. Main exterior / best land overview
2. Other exterior angles / aerials
3. Kitchen, living, primary bed, baths
4. Other rooms / land features
5. Backyard, garage, driveway
6. Street/context if helpful

Result: `images` array rewritten in best-first order; first item becomes the intended primary/cover photo.

### Failure handling

If the curation job throws an exception:

| Field | Value |
|-------|-------|
| `status` | `image_curation_failed` |
| `rules_ai_reason` | `image_curation_failed: <error>` |

---

## Stage 2 — Primary image verification

A stricter **dual-model** check on `images[0]` before posting.

### Flow

```
ready_for_primary_image_check
    │
    ├─ no images?
    │     → ready_to_post (edge case — same as empty curation path)
    │
    └─ classify images[0] with TWO models:
          gpt-5.1 (primary)
          gpt-5-mini (secondary)
          │
          ├─ BOTH keep=true  → ready_to_post
          └─ either rejects   → primary_image_failed
```

Both models must agree the primary image is a genuine property photo. This reduces false positives where a logo or flyer slipped through curation.

### Audit field

Full results are stored in `primary_image_check`:

```json
{
  "url": "https://...",
  "keep": true,
  "reason": "both_models_keep_true",
  "model_primary": { "name": "gpt-5.1", "keep": true, "reason": "property exterior" },
  "model_secondary": { "name": "gpt-5-mini", "keep": true, "reason": "property exterior" }
}
```

On failure, `reason` is `one_or_both_models_rejected` with per-model details.

---

## Status summary

| Status | Meaning |
|--------|---------|
| `ready_for_image_processing` | Waiting for curation job |
| `ready_for_primary_image_check` | Curation done; waiting for primary verification |
| `ready_to_post` | Passed all image checks (or had no images) |
| `primary_image_failed` | Primary photo rejected by one or both models |
| `image_curation_failed` | Curation job crashed |

---

## Fields read and written

### Curation job

| Field | Action |
|-------|--------|
| `images` | Replaced with filtered, ordered URLs |
| `skipped_images` | Array of `{url, reason}` for removed images |
| `status` | → `ready_for_primary_image_check` or `ready_to_post` |

### Primary check job

| Field | Action |
|-------|--------|
| `primary_image_check` | Full dual-model audit object |
| `status` | → `ready_to_post` or `primary_image_failed` |

---

## Return values

**Curation:**

```python
{ "total": int, "curated": int, "no_images": int, "failed": int }
```

**Primary check:**

```python
{ "total": int, "checked": int, "passed": int, "failed": int, "no_image": int, "errors": [...] }
```

---

## Pipeline position

```
passed
    ↓  (post selection)
ready_for_image_processing
    ↓  ← Stage 1: curation
ready_for_primary_image_check
    ↓  ← Stage 2: primary verification
ready_to_post
    ↓  (WhatsApp ad generation — see whatsapp_ad_generation.md)
posted
```

See also:

- [Post selection](./post_selection.md) — previous stage
- [WhatsApp ad generation](./whatsapp_ad_generation.md) — uses `images[0]` as the message image

---

## Downstream usage

| Consumer | Uses |
|----------|------|
| `ai_make_whatsapp_posts.py` | Listing data; first HTTPS image sent via Node gateway |
| `whatsapp_sender.py` | `_first_image_url(images)` for `imageUrl` |
| WordPress pipeline | Curated `images` array for property media |

---

## Common issues

| Symptom | Likely cause | What to check |
|---------|--------------|---------------|
| `primary_image_failed` | Cover photo is logo/flyer/map | `primary_image_check`, `skipped_images` |
| All images filtered out | Over-aggressive vision classification | `skipped_images` reasons; original email images |
| Stuck on `ready_for_image_processing` | Job backlog (limit 5/run) | Scheduler logs, queue depth |
| Listing posted with no image | Empty `images` after curation | Curation result; WhatsApp still sends text-only |
| `image_curation_failed` | OpenAI API error | `rules_ai_reason`, API key/quota |

---

## Key files

```
py_RichListings/
├── image_curation.py        # Both curation and primary check
├── server_runner.py         # Scheduler (both jobs every 2 min)
└── models/__init__.py       # ParsedListing (images, skipped_images, primary_image_check)
```

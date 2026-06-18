# Media Verification

This document describes **Stage A** of the qualification pipeline: how listings recover property photos and gallery links from source emails, fix broken image URLs, and advance to duplicate detection.

For the full pipeline context, see [architecture.md](./architecture.md).

---

## Purpose

When a listing is first parsed from email, it may have incomplete media:

- Direct image URLs in `images` may be missing or empty
- A “more pictures” / gallery link in `other_images_source` may be missing
- Image hosts may block hotlinking (HTTP 403) unless a browser User-Agent is used

Media verification ensures every listing has been checked for media before deduplication and AI rules run. It also sets `wp_check = "pending"` so later WordPress jobs can track price/media updates for reposts.

---

## File and entry point

| Item | Value |
|------|-------|
| **Module** | `ai_media_verify.py` |
| **Function** | `verify_and_fill_missing_media_for_not_processed()` |
| **Scheduler** | `server_runner.py` → `run_verify_and_fill_missing_media_for_not_processed` |
| **Interval** | Every **3 minutes** |
| **Batch size** | Up to **35** listings per run, **8** parallel workers |

---

## Status transition

```
not_processed  →  verified
```

Every listing processed by this job ends in `verified`, regardless of whether AI found new media.

---

## Processing logic

For each `ParsedListing` with `status = "not_processed"`:

### Path 1 — Both media fields already present

If the listing already has:

- At least one URL in `images`, **and**
- A non-empty `other_images_source`

Then:

1. Run **403/S3 fix** on existing `images` (see below)
2. Set `status = "verified"` and `wp_check = "pending"`
3. **Skip OpenAI** — no re-scan of the email body

### Path 2 — One or both fields missing

1. Load the source email body from `source_email.bodies`:
   - Prefer `html_ai` (markdown converted from HTML)
   - Fallback to `html_full`
2. Call OpenAI with the listing address as an anchor to locate the correct section in a multi-listing email
3. Fill **only missing fields** — never overwrite existing `images` or `other_images_source`
4. Run **403/S3 fix** on any new or existing image URLs before saving
5. Set `status = "verified"` and `wp_check = "pending"`

If the email body is empty or AI finds nothing, the listing is still marked `verified` with whatever media it already had.

---

## OpenAI media extraction

**Model:** `OPENAI_MODEL` env var (default `gpt-4.1`)

The model receives:

- The listing address (street, city, state, zip)
- The email markdown body

It returns structured JSON:

| Field | Description |
|-------|-------------|
| `matched` | Whether the listing section was found in the email |
| `images` | Direct image URLs (`http`/`https`), most relevant first, capped at **12** |
| `other_images_source` | Single gallery / “more pictures” link (Google Drive, Dropbox, etc.) |
| `notes` | Optional debug notes |

**Rules enforced in the prompt:**

- Extract URLs from the listing section only — ignore logos, signatures, QR codes, unsubscribe links
- Do not invent URLs
- Return the gallery link verbatim when present

---

## Hotlink / 403 handling (S3 mirror)

Many wholesaler image hosts return **403 Forbidden** to plain HTTP requests. The module handles this in `_fix_forbidden_images()`:

1. Try a plain `GET` — if **200**, keep the original URL
2. Retry with a Chrome User-Agent header
3. If that succeeds, upload bytes to **AWS S3** and replace the URL with the public S3 URL
4. If all attempts fail, keep the original URL (better than dropping the image)

**Required env vars:**

| Variable | Purpose |
|----------|---------|
| `LISTINGS_S3_BUCKET` | S3 bucket name |
| `LISTINGS_S3_PREFIX` | Key prefix (default `images/`) |
| `AWS_REGION` | Region (default `us-east-1`) |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Credentials (via standard boto3 chain) |

---

## Fields read and written

### Read

| Field | Source |
|-------|--------|
| `address`, `city`, `state`, `zip` | Address anchor for AI |
| `images`, `other_images_source` | Current media state |
| `source_email.bodies.html_ai` / `html_full` | Email content for re-scan |

### Written

| Field | When |
|-------|------|
| `images` | Missing images filled by AI, or 403 URLs replaced with S3 URLs |
| `other_images_source` | Missing gallery link filled by AI |
| `status` | Always set to `verified` |
| `wp_check` | Always set to `pending` |
| `updated_at` | UTC timestamp |

---

## Return value (job stats)

```python
{
    "total_not_processed": int,      # candidates in queue
    "scanned": int,
    "verified_direct": int,          # had both fields; no AI
    "verified_ai_path": int,         # ran AI path (even if nothing new found)
    "updated_missing_fields": int,   # actually filled a missing field
    "errors": [...]                  # up to 20 error strings
}
```

---

## Pipeline position

```
Email parsed
    ↓
not_processed
    ↓  ← YOU ARE HERE (media verify)
verified
    ↓  (30-day dedup — see dedup_30_day.md)
processed / skipped
```

See also:

- [30-day dedup](./dedup_30_day.md)
- [Post selection](./post_selection.md) — runs much later, after AI rules

---

## Common issues

| Symptom | Likely cause | What to check |
|---------|--------------|---------------|
| Listing stuck on `not_processed` | Media verify job not running | `server_runner.py` logs, `verify_and_fill_missing_media_for_not_processed` |
| `images` empty after verify | No photos in email, or AI could not match address block | Source email HTML, listing address accuracy |
| Images 403 in WhatsApp/WP | S3 upload failed or bucket not configured | `LISTINGS_S3_BUCKET`, AWS credentials, logs |
| Gallery link missing | Wholesaler did not include one in email | `other_images_source` stays empty; listing still verifies |

---

## Key files

```
py_RichListings/
├── ai_media_verify.py       # This stage
├── server_runner.py         # Scheduler (every 3 min)
├── models/__init__.py       # ParsedListing schema
└── processFilteredEmail.py  # Creates not_processed listings
```

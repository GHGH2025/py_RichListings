# WhatsApp Ad Generation

This document describes **Stage F — ad copy generation**: how listings in `ready_to_post` get AI-written WhatsApp messages, are marked `posted`, and enter the send queue.

For message **delivery** (Node gateway, DM vs group, retries), see [whatsapp.md](./whatsapp.md).

For the full pipeline context, see [architecture.md](./architecture.md).

---

## Purpose

Once a listing passes image curation and reaches `ready_to_post`, this stage:

1. Reads human-written formatting rules from `ad_post_rules.txt`
2. Sends listing data to OpenAI to compose a WhatsApp-friendly ad
3. Saves the result in `post_content`
4. Marks the listing `posted` and queues WhatsApp delivery (`whatsapp_status = pending`)
5. Starts the parallel WordPress pipeline (`wp_status = ready_to_process`)
6. Fires the posted-listing webhook (Podio Flow, if configured)

**Important:** This job writes the message and updates status. It does **not** send WhatsApp directly — that is handled by `whatsapp_sender.py` on a separate 1-minute schedule.

---

## File and entry point

| Item | Value |
|------|-------|
| **Module** | `ai_make_whatsapp_posts.py` |
| **Function** | `make_whatsapp_posts_from_ready_to_post(rules_path, limit)` |
| **Rules file** | `ad_post_rules.txt` |
| **Scheduler** | `server_runner.py` → `run_make_whatsapp_posts_from_ready_to_post` |
| **Interval** | Every **2 minutes** |
| **Batch size** | Up to **5** listings per run |
| **AI model** | `OPENAI_MODEL` env var (default `gpt-4o-mini`) |

---

## Status and parallel field transitions

```
ready_to_post
    ↓  (this job — success)
posted
    + post_content: "<WhatsApp message>"
    + whatsapp_status: pending
    + wp_status: ready_to_process
    + skipped_or_posted_at: now
```

On AI failure, status stays `ready_to_post` and `rules_ai_reason` is set to `post_generation_failed: ...`.

---

## Input data sent to OpenAI

The model receives a JSON listing object built from:

| Field | Notes |
|-------|-------|
| `complete_info` | Primary source for property details (~50+ extracted fields) |
| `address`, `city`, `state`, `zip`, `price` | Top-level fallbacks |
| `images` | Curated image URLs |
| `other_images_source` | Original gallery link |
| `other_images_dropbox_link` | Dropbox shared folder (from post selection) |
| `sender_name_hint` | Wholesaler name for tone only — must not appear in output |

Plus the full verbatim text of `ad_post_rules.txt`.

---

## Rules file highlights (`ad_post_rules.txt`)

Human rules the AI must follow:

**Must include:**

- Property description and price when present
- **Bold** full address line (WhatsApp: `*address*`)
- **Bold** price line (e.g. `*Price: $450,000*`)
- `other_images_dropbox_link` on its own line immediately under price (no label)
- All factual property details explicitly stated in source (comps, condition, amenities)
- Short, sales-friendly bullets

**Must never include:**

- EMD / escrow amount, ARV, closing dates
- Title/escrow company names
- Phone numbers, emails, websites, QR codes
- Emojis, company names, personal names

**Formatting:**

- US dollar format, no cents, with commas
- Skip unknown fields — do not invent data
- Anti-hallucination: only include features explicitly in source text

The system prompt maps Markdown `**bold**` in rules to WhatsApp `*bold*`.

---

## OpenAI call

**System prompt:** Instructs the model to produce WhatsApp copy using only listing data, follow rules exactly, and return JSON.

**User prompt:** Contains rules text + full listing JSON.

**Response format:**

```json
{ "post_content": "🏠 *123 Main St, Miami, FL*\n\n*Price: $450,000*\n\n..." }
```

Temperature: `0.2`. Empty `post_content` raises an error and counts as failure.

---

## Side effects on success

| Action | Detail |
|--------|--------|
| Save `post_content` | Final WhatsApp message text |
| Set `status = posted` | Main pipeline terminal for posting path |
| Set `whatsapp_status = pending` | Queued for `whatsapp_sender.py` |
| Set `wp_status = ready_to_process` | Starts WP mapper/description/sync jobs |
| Set `skipped_or_posted_at` | UTC timestamp |
| Clear `rules_ai_reason` | Clears prior errors |
| Webhook | `POSTED_LISTING_WEBHOOK_URL` if configured |

### Posted webhook payload

```json
{
  "event": "listing_posted",
  "listing": {
    "id": "...",
    "status": "posted",
    "post_content": "...",
    "images": [...],
    "complete_info": {...},
    ...
  }
}
```

Best-effort — webhook failure does not roll back the listing.

---

## Send queue (next step)

After this job, `whatsapp_sender.process_whatsapp_queue()` runs every **1 minute**:

1. Finds `whatsapp_status` in `pending` or `failed`
2. POSTs to Node gateway with `text = post_content` and `imageUrl = first HTTPS image from images`
3. Sets `whatsapp_status` to `sent` or `failed`

See [whatsapp.md](./whatsapp.md) for DM/group mode, gateway setup, and troubleshooting.

---

## Return value (job stats)

```python
{ "total": int, "posted": int, "failed": int }
```

---

## Pipeline position

```
ready_for_primary_image_check
    ↓  (primary image check)
ready_to_post
    ↓  ← YOU ARE HERE (WhatsApp ad generation)
posted  (+ whatsapp_status: pending, wp_status: ready_to_process)
    ↓  (whatsapp_sender — every 1 min)
whatsapp_status: sent
    ↓  (parallel)
wp_status: ready_to_process → keys_generated → description_generated → posted
```

See also:

- [Image curation](./image_curation.md) — previous stage
- [whatsapp.md](./whatsapp.md) — delivery layer

---

## Environment variables

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Required |
| `OPENAI_MODEL` | Chat model for ad copy |
| `POSTED_LISTING_WEBHOOK_URL` | Optional Podio/automation webhook |
| `TEAM_WHATSAPP_NUMBERS` | Used by send queue, not this job |

---

## Common issues

| Symptom | Likely cause | What to check |
|---------|--------------|---------------|
| Stuck on `ready_to_post` | AI generation failed | `rules_ai_reason`, OpenAI quota |
| `post_content` missing disallowed fields | Model ignored rules | `ad_post_rules.txt`, listing source data |
| No Dropbox link in post | Missing `other_images_dropbox_link` | [Post selection](./post_selection.md) Dropbox upload |
| `posted` but no WhatsApp message | Send queue not running or gateway down | `whatsapp_status`, [whatsapp.md](./whatsapp.md) |
| Webhook not firing | `POSTED_LISTING_WEBHOOK_URL` unset or failing | Env var, server logs `[webhook]` |

---

## Key files

```
py_RichListings/
├── ai_make_whatsapp_posts.py  # This stage
├── ad_post_rules.txt          # Human formatting rules for AI
├── whatsapp_sender.py         # Send queue (next step)
├── server_runner.py           # Scheduler (every 2 min, limit 5)
└── config_runtime.py          # WhatsApp send mode (used by sender)
```

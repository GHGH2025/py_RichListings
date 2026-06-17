# WordPress Integration

This document explains how RichListings publishes property listings to the **Buyers List inventory website** on WordPress.

**Live site:** `https://inventory.joinbuyerslist.com`

RichListings does not log into WordPress manually. It calls a custom REST API plugin (`addproperty/v1`) to search for existing properties and create or update posts.

---

## Big picture flow

```
Listing posted to WhatsApp
    ↓
AI maps property type / region / taxonomy
    ↓
AI writes HTML property description
    ↓
Sync job searches WP → creates post if not found
    ↓
Property appears on inventory site

(Separate track)
Listing verified → price/media update job checks WP for changes
```

WordPress publishing starts **after** a listing is marked `posted` (WhatsApp ad copy is generated). The two pipelines share the same listing but use different MongoDB fields.

---

## Step-by-step: from listing to WordPress post

### Phase 0 — Before WordPress (upstream)

These steps happen before WordPress is involved:

| Step | Job | Result |
|------|-----|--------|
| Email parsed | `processFilteredEmail` | New `ParsedListing` in MongoDB |
| Rules applied | `apply_ai_english_rules` | `status = "passed"` or `"skipped"` |
| Media verified | `verify_and_fill_missing_media` | `status = "verified"`, `wp_check = "pending"` |
| Selected for post | `select_passed_listings_for_post` | `status = "ready_for_image_processing"` |
| Images curated | `process_listings_ready_for_image_processing` | Images ready |
| Primary image OK | `process_primary_image_verification` | `status = "ready_to_post"` |

### Phase 1 — WhatsApp post triggers WordPress

**File:** `ai_make_whatsapp_posts.py`  
**Runs:** every **2 minutes**

When AI writes the WhatsApp ad:

- `status = "posted"`
- `post_content` = WhatsApp message text
- **`wp_status = "ready_to_process"`** ← WordPress pipeline starts here
- `whatsapp_status = "pending"`

The WhatsApp message text is later reused as input for the WordPress description.

### Phase 2 — AI taxonomy mapping

**File:** `wp_ai_mapper_catalog_first.py`  
**Runs:** every **3 minutes**

1. Finds listings with `wp_status = "ready_to_process"`.
2. Sends listing data to OpenAI.
3. OpenAI returns taxonomy fields: region, property type, country deals, etc.
4. Saves result in `wp_parsed_data`.
5. Sets **`wp_status = "keys_generated"`**.

Example `wp_parsed_data` fields:

- `country_deals`
- `region`
- `property_name`
- `proposed_*` taxonomy values

### Phase 3 — AI property description

**File:** `wp_ai_property_description.py`  
**Runs:** every **3 minutes**

1. Finds listings with `wp_status = "keys_generated"`.
2. Uses `complete_info` + `post_content` to generate HTML description.
3. Saves HTML in `wp_property_description`.
4. Sets **`wp_status = "des_generated"`**.

### Phase 4 — Sync to WordPress (create or find existing)

**File:** `wp_sync_poster.py`  
**Runs:** every **5 minutes**

1. Finds listings with `wp_status = "des_generated"` and a non-empty description.
2. **Searches WordPress** by address:
   - Tries `"<address>, <city>"` first
   - Then tries each entry in `address_search_keys`
3. **If property already exists in WP:**
   - Sets `wp_status = "already_found"`
   - Saves existing `post_id`
   - Does not create a duplicate
4. **If not found:**
   - Builds create payload (title, address, description, price, image, region, etc.)
   - Calls `POST /create`
   - On success: `wp_status = "posted"`, `post_id = <new WordPress post ID>`
   - On failure: stays at `des_generated` or `wp_status = "failed"`

### Phase 5 — Price and media updates (separate track)

**File:** `wp_price_red_pic_links.py`  
**Runs:** every **2 minutes**

This runs in parallel — it does not wait for the create pipeline.

1. Finds listings with `wp_check = "pending"`.
2. Searches WordPress for a matching property by address.
3. Uses AI to confirm the match (confidence must be ≥ 80%).
4. **Price reduction:** If WP price is higher than parsed price → updates WP with `REDUCED!!` in title.
5. **Missing gallery link:** If WP has no `picture_button_url` → adds Dropbox gallery link.
6. Sets `wp_check = "processed"`, `"not_found"`, or `"found_but_rejected"`.

---

## WordPress API

**Base URL:** `https://inventory.joinbuyerslist.com/wp-json/addproperty/v1`  
**Auth:** `WP_API_TOKEN` (sent as `token` in query string or JSON body)

### GET `/getproperty`

Search for an existing property by address.

```
GET /getproperty?address=123%20Main%20St,%20Miami&token=<WP_API_TOKEN>
```

Response (simplified):

```json
{
  "success": true,
  "data": [{
    "post_id": 12345,
    "address": "123 Main St, Miami",
    "asking_price": "250000",
    "picture_button_url": "https://...",
    "posttitle": "3/2 SFH Miami"
  }]
}
```

### POST `/create`

Create a new property **or** update an existing one (same endpoint for both).

**Create payload** (from `wp_sync_poster.py`):

| Field | Source |
|-------|--------|
| `posttitle` | Composed from address + city |
| `address` | Listing address + city |
| `postdesc` | `wp_property_description` (AI HTML) |
| `featured_image` | First image URL from listing |
| `asking_price` | Listing price |
| `zip_code` | Listing zip |
| `country_deals` | From `wp_parsed_data` |
| `region` | From `wp_parsed_data` |
| `property_name` | From `wp_parsed_data` |
| `picture_button_url` | Dropbox gallery link |
| `newest_deals` | `["Todays Deal"]` |

**Update payload** (from `wp_price_red_pic_links.py`):

- Price reduction: adds `REDUCED!!` prefix to title
- Media update: sends `picture_button_url` only

Success response:

```json
{ "post_id": 12345 }
```

---

## MongoDB fields (`parsed_listings`)

### Main WordPress pipeline

| Field | Values | Meaning |
|-------|--------|---------|
| `wp_status` | See table below | Where listing is in WP pipeline |
| `wp_parsed_data` | object | AI taxonomy (region, property type, etc.) |
| `wp_property_description` | HTML string | AI-generated description for WP |
| `post_id` | integer | WordPress post ID after sync |
| `post_content` | text | WhatsApp message (input to description AI) |
| `address_search_keys` | list | Alternate address strings for WP search |
| `other_images_dropbox_link` | URL | Gallery link → WP `picture_button_url` |

**`wp_status` progression:**

```
ready_to_process
    ↓ (taxonomy AI)
keys_generated
    ↓ (description AI)
des_generated
    ↓ (sync)
posted          ← new property created in WP
already_found   ← property already existed in WP
failed          ← missing address/city or unrecoverable error
```

### Price/media update track

| Field | Values | Meaning |
|-------|--------|---------|
| `wp_check` | `pending`, `processed`, `not_found`, `found_but_rejected` | Update job status |
| `wp_check_post_id` | integer | WP post ID found during check |
| `wp_check_reduced` | `"updated"` | Price reduction was applied |

---

## Scheduled jobs

From `server_runner.py`:

| Job | Interval | Function | Limit |
|-----|----------|----------|-------|
| **`run_ai_build_wp_payload_for_posted`** | **3 min** | Taxonomy AI | 5 |
| **`run_ai_build_wp_property_description_for_posted`** | **3 min** | Description AI | 5 |
| **`run_sync_wp_for_descriptions`** | **5 min** | Create/find in WP | 5 |
| **`run_process_wp_price_and_media_updates`** | **2 min** | Price/media updates | 5 |

Upstream job that starts the pipeline:

| Job | Interval | Sets |
|-----|----------|------|
| `run_make_whatsapp_posts_from_ready_to_post` | 2 min | `wp_status = "ready_to_process"` |
| `run_verify_and_fill_missing_media` | (varies) | `wp_check = "pending"` |

---

## Environment variables

| Variable | Purpose |
|----------|---------|
| `WP_API_TOKEN` | **Required.** Auth token for all WP API calls |
| `WP_API_BASE` | Base URL (default: `https://inventory.joinbuyerslist.com/wp-json/addproperty/v1`) |
| `OPENAI_API_KEY` | Required for AI taxonomy and description steps |
| `OPENAI_MODEL` | Model override (defaults vary by file) |
| `DROPBOX_APP_KEY` | For gallery link generation |
| `DROPBOX_APP_SECRET` | For gallery link generation |
| `DROPBOX_REFRESH_TOKEN` | For gallery link generation |
| `MONGO_URI` | MongoDB connection |

**Note:** `wp_price_red_pic_links.py` hardcodes the WP URL and does not read `WP_API_BASE`.

---

## How to verify WordPress is working

### 1. Check MongoDB pipeline position

```javascript
// Waiting for taxonomy AI
db.parsed_listings.find({ wp_status: "ready_to_process" }).limit(5)

// Waiting for description AI
db.parsed_listings.find({ wp_status: "keys_generated" }).limit(5)

// Waiting for sync to WP
db.parsed_listings.find({ wp_status: "des_generated" }).limit(5)

// Successfully on WordPress
db.parsed_listings.find({ wp_status: "posted", post_id: { $exists: true } }).limit(5)

// Price/media track
db.parsed_listings.find({ wp_check: "pending" }).count()
db.parsed_listings.find({ wp_check_reduced: "updated" }).limit(5)
```

### 2. Check server logs

Look for:

- `ai_build_wp_payload_for_posted`
- `ai_build_wp_property_description_for_posted`
- `sync_wp_for_descriptions`
- `run_sync_wp_for_descriptions: result={...}` — includes counts: `processed`, `posted`, `already_found`, `errors`

### 3. Test the WordPress API directly

```bash
curl "https://inventory.joinbuyerslist.com/wp-json/addproperty/v1/getproperty?address=123%20Main%20St,%20Miami&token=$WP_API_TOKEN"
```

### 4. Check the live site

Open `https://inventory.joinbuyerslist.com` and search for a recently posted address.

Use the `post_id` from MongoDB to find the exact post if needed.

### 5. Worker health

```http
GET http://<host>:8000/server-status
```

Confirms the scheduler is running (does not report WP-specific status).

### 6. Replay a single listing (Python shell)

```python
from wp_ai_mapper_catalog_first import ai_build_wp_payload_by_id
from wp_ai_property_description import ai_build_wp_property_description_by_id
from wp_sync_poster import sync_wp_for_descriptions

# Replace with a real listing ID
listing_id = "..."

ai_build_wp_payload_by_id(listing_id)
ai_build_wp_property_description_by_id(listing_id)
sync_wp_for_descriptions(limit=1)
```

---

## Common problems

| Problem | Likely cause | What to do |
|---------|--------------|------------|
| `RuntimeError: WP_API_TOKEN is not set` | Missing env var | Add `WP_API_TOKEN` to `.env` |
| Stuck at `ready_to_process` | OpenAI error in taxonomy step | Check server logs; verify `OPENAI_API_KEY` |
| Stuck at `keys_generated` | OpenAI error in description step | Check server logs |
| Stuck at `des_generated` | WP POST failed | Check logs for `WP POST failed`; test API with curl |
| `wp_status = "failed"` | Missing address or city | Listing needs both fields for WP title |
| `already_found` but expected new post | WP already has a property at that address | Normal — duplicate prevention |
| `wp_check = "found_but_rejected"` | AI address match confidence below 80% | Address in email may not match WP record |
| `wp_check = "not_found"` | No WP match for any address key | Property may not exist on site yet |
| Price reduction skipped | WP price is already ≤ parsed price | No reduction needed |
| Gallery link not added | WP already has `picture_button_url`, or no Dropbox link | Check `other_images_dropbox_link` |
| Dropbox errors | Missing or invalid `DROPBOX_*` env vars | Fix Dropbox credentials |

---

## Relationship to WhatsApp and Podio

| System | When it connects | Field that links them |
|--------|------------------|----------------------|
| **WhatsApp** | Runs first — marks listing `posted` | `wp_status = "ready_to_process"` is set at same time |
| **Podio** | Posted webhook fires when WhatsApp post is created | Independent of WP pipeline |
| **WordPress** | Starts after WhatsApp post | `wp_status` tracks progress |

All three can run at the same time on the same listing. They use different MongoDB fields and do not block each other.

---

## Key files

```
py_RichListings/
├── server_runner.py              # Scheduled jobs
├── ai_make_whatsapp_posts.py     # Sets wp_status = ready_to_process
├── wp_ai_mapper_catalog_first.py # Step 1: taxonomy AI
├── wp_ai_property_description.py # Step 2: description AI
├── wp_sync_poster.py             # Step 3: create/find in WP
├── wp_price_red_pic_links.py     # Price/media update track
├── ai_media_verify.py            # Sets wp_check = pending
└── models/__init__.py            # wp_* field definitions
```

For RingCentral media linking (also uses the same WP API), see `rc_media_linker.py`.

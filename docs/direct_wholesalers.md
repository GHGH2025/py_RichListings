# Direct Wholesalers (MongoDB + Podio mapping)

Direct wholesaler contacts live in MongoDB and are exposed via a CRUD API. Workers read the map at runtime.

This collection answers: **when we scrape a deal from email, which wholesaler is it, and how do we attach that wholesaler on Podio?**

For Podio app IDs, scheduled jobs, and troubleshooting, see also [podio.md](./podio.md).

---

## End-to-end flow (email scrape → Podio)

Mapping is **lookup-and-link**, not create-on-miss. RichListings does **not** create new Wholesellers or Properties items in Podio.

```
Gmail From
    → Mongo `direct_wholesalers` (by sender_email)
    → stamp agent_* + direct_wholeseller=not_processed on ParsedListing
    → cron every 3 min
    → find Active Podio property by address
    → find Wholesellers item by contact email
    → set property Wholeseller reference (if updateFlagForPodio)
```

### Phase A — Identify wholesaler from the email sender (Mongo)

1. Gmail fetch stores the message; AI parse creates `ParsedListing` rows.
2. On parse/upsert (`pipeline/listing_details.py`), take Gmail **From** (`from_info.email`, lowercased).
3. Look up that address in `get_wholesaler_map()` keyed by `sender_email`.
4. **Hit:**
   - Overwrite listing `agent_name`, `agent_phone`, `agent_email` from the map (contact `email` may differ from Gmail From / ccsend proxies).
   - Copy `updateFlagForPodio` onto the listing blob.
   - Set `ParsedListing.direct_wholeseller`:
     - `"not_processed"` if the listing is queued for normal processing
     - `"bypassed"` if that listing index was sliced out of processing
5. **Miss:** set `direct_wholeseller = "not_found"` — never enters the Podio linker.

### Phase B — Link on Podio (cron)

Every **3 minutes**, `server_runner` → `process_direct_wholeseller_batch()` in `integrations/podio/direct_wholesaler.py`:

1. Load listings with `direct_wholeseller = "not_processed"`.
2. Read `complete_info.agent_email` and `updateFlagForPodio`.
3. Find an **Active** property in the Podio Properties app by address + city (plus address search keys; strict match).
4. Look up the Wholesellers app by **email only** (`agent_email`).
5. If found and `updateFlagForPodio` is true: `PUT` the property’s Wholeseller reference field.
6. If `updateFlagForPodio` is false: skip the Podio write but still mark the listing `processed`.
7. Update `ParsedListing.direct_wholeseller` to the outcome status below.

**Important:** The property must already exist in Podio as Active. This job only updates the Wholeseller link.

### Matching identifiers

| Stage | Identifier | Used? |
|-------|------------|-------|
| Mongo direct-wholesaler ID | Gmail **From** → `sender_email` | Yes — “is this a direct wholesaler?” |
| Listing contact after override | Map’s **`email`** → `agent_email` | Yes — Podio Wholesellers search |
| Podio Wholesellers | Email field only | Yes — exact lowercased match |
| Podio property | Address + city, Status = Active | Yes — which property to update |
| Name / phone | Stored on listing | No Podio match |
| Domain | Scraping allow/skip only | No Podio match |

### `ParsedListing.direct_wholeseller` statuses

| Value | Meaning |
|-------|---------|
| `not_processed` | Known sender; waiting for Podio linker |
| `processed` | Linked (or already correct / write skipped by flag) |
| `not_found` | Gmail From not in `direct_wholesalers` |
| `bypassed` | Known sender, but this listing index was not queued for processing |
| `no_agent_email` | No `agent_email` on the listing |
| `property_not_found` | No matching Active property in Podio |
| `wholeseller_not_found` | Property found; no Wholesellers item with that email |

On miss, **nothing is auto-created** in Podio. Ops must add the wholeseller (and/or Mongo map entry) manually.

### How to check

- MongoDB: `db.parsed_listings.find({ direct_wholeseller: "processed" })`
- Logs: `run_direct_wholeseller_linking`, `Setting Wholeseller reference on property item ...`
- Podio UI: open the property → Wholeseller field
- Config API: `/api/direct-wholesalers`

### Adding a new wholesaler (ops checklist)

Nothing auto-creates the Podio Wholesellers record or the Mongo map. Do these in order:

#### 1. Create (or confirm) the Podio Wholesellers item

In the **Wholesellers** app, create a contact (or reuse an existing one) whose **Email** field is the real contact email you will use for matching (not a `*.ccsend.com` proxy unless that is truly what Podio has).

RichListings matches Podio wholesellers by that email **exactly** (lowercased). If this step is missing, listings will end as `wholeseller_not_found`.

#### 2. Allow their emails to be scraped (if needed)

If Gmail fetch uses an allow list for that account, add their From / domain to `scraping_list` so messages are ingested at all. See [scraping_list.md](./scraping_list.md).

```bash
curl -X POST http://localhost:8000/api/scraping-list \
  -H "Content-Type: application/json" \
  -d '{
    "account_label": "acct1",
    "sender_pattern": "manny@homeventureinvestments.com",
    "list_type": "allow",
    "active": true
  }'
```

Use a domain pattern (e.g. `@theircompany.com`) when many senders share one domain. Skip this if they already match an existing allow pattern.

#### 3. Add them to Mongo `direct_wholesalers`

| Field | What to put |
|-------|-------------|
| `sender_email` | Exact Gmail **From** on their blast emails (often a ccsend / proxy address) |
| `email` | Same contact email as the Podio Wholesellers **Email** field |
| `name` / `phone` | Contact to stamp onto listings |
| `updateFlagForPodio` | `true` to write the Podio Wholeseller link; `false` to identify only |

```bash
curl -X POST http://localhost:8000/api/direct-wholesalers \
  -H "Content-Type: application/json" \
  -d '{
    "sender_email": "sguerrero-housingig.com@shared1.ccsend.com",
    "email": "sguerrero@housingig.com",
    "name": "S Guerrero",
    "phone": "555-123-4567",
    "updateFlagForPodio": true
  }'
```

Or look up / patch by sender: `GET /api/direct-wholesalers/by-sender/{sender_email}`.

**Tip:** Open one of their emails in Gmail and copy the From address into `sender_email`. Put the real reply-to / agent email into `email` (must match Podio).

#### 4. Wait for a deal (or reprocess)

On the next scrape/parse from that From address:

1. Listing gets `agent_*` overwritten from the map and `direct_wholeseller = "not_processed"`.
2. Within ~3 minutes the Podio job finds the Active property by address and sets the Wholeseller ref.

The property must already exist in Podio as **Active**. This flow does not create properties.

#### 5. Verify

| Check | Expected |
|-------|----------|
| Mongo listing | `direct_wholeseller: "processed"` |
| Listing agent fields | Map’s name / phone / email |
| Podio property | Wholeseller field points at the new contact |
| Failures | `not_found` → bad/missing Mongo `sender_email`; `wholeseller_not_found` → Podio email mismatch; `property_not_found` → no Active property for that address |

#### Optional extras

| Need | Where |
|------|--------|
| Special-avail inventory snapshots for this wholesaler | Add/update [special_avail_list](./special_avail_list.md) (separate from per-deal linking) |
| WhatsApp inbound from their group | See checklist in [whatsapp_inbound_ingestion.md](./whatsapp_inbound_ingestion.md) |

---

## Why two email fields

Gmail sender addresses often differ from the wholesaler's contact email (e.g. Constant Contact / ccsend proxy senders):

| Field | Purpose | Example |
|-------|---------|---------|
| `sender_email` | Gmail **From** address used for lookup | `sguerrero-housingig.com@shared1.ccsend.com` |
| `email` | Agent contact email written into listings / used for Podio | `sguerrero@housingig.com` |

---

## MongoDB collection

**Collection:** `direct_wholesalers`

| Field | Type | Notes |
|-------|------|-------|
| `sender_email` | string | Required, unique, lowercase |
| `email` | string | Required, contact email (Podio match key) |
| `name` | string | Required |
| `phone` | string | Optional |
| `updateFlagForPodio` | boolean | When `true`, Podio field update + Gmail label logic applies |
| `created_at` | datetime | Auto-set |
| `updated_at` | datetime | Auto-set on changes |

**Model:** `models/direct_wholesaler.py`  
**Service:** `services/direct_wholesaler_service.py`

---

## API

Start the API:

```bash
./run-api.sh
```

Base URL: `http://localhost:8000`  
Router prefix: `/api`  
Tag: `direct-wholesalers`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/direct-wholesalers` | List all. Optional query: `?updateFlagForPodio=true` |
| GET | `/api/direct-wholesalers/{id}` | Get by Mongo `_id` |
| GET | `/api/direct-wholesalers/by-sender/{sender_email}` | Lookup by Gmail sender |
| POST | `/api/direct-wholesalers` | Create (409 if `sender_email` exists) |
| PUT | `/api/direct-wholesalers/{id}` | Full replace |
| PATCH | `/api/direct-wholesalers/{id}` | Partial update |
| DELETE | `/api/direct-wholesalers/{id}` | Delete |
| POST | `/api/direct-wholesalers/import-json` | Upsert all entries from `direct_wholeseller.json` |

### Create / update body

```json
{
  "sender_email": "manny@homeventureinvestments.com",
  "email": "manny@homeventureinvestments.com",
  "name": "Manny",
  "phone": "754-214-0734",
  "updateFlagForPodio": true
}
```

Emails are normalized to lowercase on write.

### Example responses

**List:**

```json
[
  {
    "id": "...",
    "sender_email": "manny@homeventureinvestments.com",
    "email": "manny@homeventureinvestments.com",
    "name": "Manny",
    "phone": "754-214-0734",
    "updateFlagForPodio": true,
    "created_at": "2026-06-09T14:43:46.924000",
    "updated_at": "2026-06-09T14:43:46.924000"
  }
]
```

**Import from JSON:**

```json
{
  "ok": true,
  "created": 66,
  "updated": 0,
  "skipped": 0,
  "total": 66
}
```

---

## Seeding / migration

One-time seed from the legacy JSON file:

```bash
python scripts/seed_direct_wholesalers.py
```

Or via API:

```bash
curl -X POST http://localhost:8000/api/direct-wholesalers/import-json
```

`direct_wholeseller.json` is kept as a backup and migration source only. Workers no longer read it at runtime.

During import, `updateFlagForPodio` strings (`"true"` / `"false"`) from JSON are converted to booleans.

---

## Workers that consume this data

| File | Behavior |
|------|----------|
| `pipeline/listing_details.py` | Matches Gmail From → overrides `agent_*` on parsed listings; sets `direct_wholeseller` |
| `integrations/podio/direct_wholesaler.py` | Cron batch: property by address + wholeseller by email → set Podio Wholeseller ref |
| `ingestion/forward_completed.py` | Applies "AI Direct Wholesaler Finder" Gmail label when `updateFlagForPodio` is true |
| `server_runner.py` | Schedules `run_direct_wholeseller_linking` every 3 minutes |

Workers call `get_wholesaler_map()` from the service layer, which returns:

```python
{
  "sender@example.com": {
    "name": "...",
    "email": "...",
    "phone": "...",
    "updateFlagForPodio": True  # bool
  }
}
```

The service caches the map in-process for 60 seconds. Cache is cleared on any API create/update/delete/import.

---

## Related but different: Special Avails

`special_avail_list` maps sender emails → wholesaler names → **preconfigured** Podio item IDs for inventory snapshots. That is **not** the per-deal Wholeseller field linking described here. See [special_avail_list.md](./special_avail_list.md).

---

## File layout

```
models/
  direct_wholesaler.py              # DirectWholesaler document

routes/
  direct_wholesaler.py              # FastAPI CRUD routes

services/
  direct_wholesaler_service.py      # DB access, cache, JSON import

pipeline/
  listing_details.py                # Sender → map → stamp listing

integrations/podio/
  direct_wholesaler.py              # Podio property + wholeseller link

ingestion/
  forward_completed.py              # Gmail label for direct wholesalers

scripts/
  seed_direct_wholesalers.py        # CLI seed script

direct_wholeseller.json             # legacy source (backup only)
```

Registered in `api_app.py` with `init_db()` on startup.

---

## Environment

Uses the same MongoDB connection as the rest of the project:

- `MONGO_URI` — connection string
- `MONGO_DB` — database name (via `mongo_helper.py`; URI may also include db name for MongoEngine)

Podio apps / fields (defaults in `integrations/podio/direct_wholesaler.py`):

- `PODIO_PROPERTIES_APP_ID` (default `18339388`)
- `PODIO_WHOLESELLERS_APP_ID` (default `18339395`)

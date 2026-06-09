# Special Avail List (MongoDB + CRUD API)

Wholesaler sender emails and Podio item IDs were previously hardcoded in `special_avails.py` as `DEFAULT_WHOLESALER_BUCKET` and `DEFAULT_WHOLESALER_BUCKET_PODIO`. They are now stored in MongoDB and exposed via a CRUD API. The special-avail pipeline reads from the database at runtime.

## What this config drives

The special-avail workflow uses wholesaler config in three stages:

1. **Snapshot** (`snapshot_yesterday_special_avail`) — groups yesterday's parsed listings by wholesaler using sender email → wholesaler name mapping.
2. **Active listings** (`process_one_special_avail_with_active_listings`) — looks up Podio item IDs for each wholesaler and fetches their active properties.
3. **Matching** (`process_one_special_avail_matching`) — compares yesterday's listings against active Podio inventory.

| Field | Used for |
|-------|----------|
| `sender_emails` | Match incoming `FilteredListingEmail.from_info.email` to a wholesaler |
| `podio_item_ids` | Query Podio for active listings under that wholesaler |
| `active` | When `false`, wholesaler is excluded from all pipeline steps |

## MongoDB collection

**Collection:** `special_avail_list`

| Field | Type | Notes |
|-------|------|-------|
| `wholesaler_name` | string | Required, unique. Display name (e.g. `Johnathan`, `Ecologic Team`) |
| `sender_emails` | string[] | Required. Lowercased on write. At least one email |
| `podio_item_ids` | int[] | Podio wholesaler/contact item IDs. May be empty |
| `active` | boolean | When `false`, ignored by workers |
| `created_at` | datetime | Auto-set |
| `updated_at` | datetime | Auto-set on changes |

**Unique index:** `wholesaler_name`

**Model:** `models/special_avail_list.py`  
**Service:** `services/special_avail_list_service.py`

## API

Start the API:

```bash
./run-api.sh
```

Base URL: `http://localhost:8000`  
Router prefix: `/api`  
Tag: `special-avail-list`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/special-avail-list` | List entries. Optional query: `active` |
| GET | `/api/special-avail-list/config` | Resolved runtime config (emails + Podio bucket) |
| GET | `/api/special-avail-list/{id}` | Get by Mongo `_id` |
| POST | `/api/special-avail-list` | Create (409 if `wholesaler_name` exists) |
| PUT | `/api/special-avail-list/{id}` | Full replace |
| PATCH | `/api/special-avail-list/{id}` | Partial update |
| DELETE | `/api/special-avail-list/{id}` | Delete |
| POST | `/api/special-avail-list/import-seed` | Upsert all entries from `special_avail_list_seed.json` |

### Create / update body

```json
{
  "wholesaler_name": "Johnathan",
  "sender_emails": [
    "info-jefinancialholdings.com@shared1.ccsend.com"
  ],
  "podio_item_ids": [857496733, 778054254],
  "active": true
}
```

`sender_emails` must contain at least one address. Emails are trimmed and lowercased on write.

### Example responses

**List:**

```bash
curl "http://localhost:8000/api/special-avail-list?active=true"
```

```json
[
  {
    "id": "...",
    "wholesaler_name": "Johnathan",
    "sender_emails": ["info-jefinancialholdings.com@shared1.ccsend.com"],
    "podio_item_ids": [857496733, 778054254],
    "active": true,
    "created_at": "2026-06-09T15:29:56.048000",
    "updated_at": "2026-06-09T15:29:56.048000"
  }
]
```

**Runtime config (what workers see):**

```bash
curl "http://localhost:8000/api/special-avail-list/config"
```

```json
{
  "wholesaler_config": {
    "Johnathan": ["info-jefinancialholdings.com@shared1.ccsend.com"]
  },
  "podio_bucket": {
    "johnathan": [857496733, 778054254]
  },
  "sender_emails": ["info-jefinancialholdings.com@shared1.ccsend.com"]
}
```

**Import from seed JSON:**

```json
{
  "ok": true,
  "created": 24,
  "updated": 0,
  "skipped": 0,
  "total": 24
}
```

## Seeding / migration

One-time seed from the legacy hardcoded lists (saved in `special_avail_list_seed.json`):

```bash
python scripts/seed_special_avail_list.py
```

Or via API:

```bash
curl -X POST http://localhost:8000/api/special-avail-list/import-seed
```

`special_avail_list_seed.json` is kept as a backup and migration source. Re-running import upserts: existing rows are updated and marked `active: true`; new wholesalers are inserted.

## Workers that consume this data

| File | Function | Schedule / trigger |
|------|----------|-------------------|
| `special_avails.py` | `build_yesterday_unique_parsed_listings_for_wholesalers()` | Called by snapshot task |
| `special_avails.py` | `process_one_special_avail_with_active_listings()` | Every 3 min (`server_runner.py`) |
| `special_avails.py` | `process_one_special_avail_matching()` | Every 5 min (`server_runner.py`) |
| `api_app.py` | `POST /tasks/snapshot-yesterday-special-avail` | Manual trigger |
| `api_app.py` | `POST /tasks/run-manny-special-avails` | Manual trigger (separate Manny sheet flow) |

Workers call `get_wholesaler_config()` and `get_wholesaler_podio_bucket()` from the service. Config is cached in-process for 60 seconds; cache is cleared on any API create/update/delete/import.

## Related collections

| Collection | Purpose |
|------------|---------|
| `special_avail` | Daily snapshot rows per wholesaler (items, active_listings, match results) |
| `special_avail_list` | Wholesaler → sender emails + Podio IDs (this doc) |

Do not confuse with `direct_wholesalers` — that collection powers the direct wholesaler Podio sync, not the special-avail pipeline.

## File layout

```
models/
  special_avail_list.py             # SpecialAvailList document

routes/
  special_avail_list.py             # FastAPI CRUD routes

services/
  special_avail_list_service.py     # DB access, cache, JSON import

scripts/
  seed_special_avail_list.py        # CLI seed script

special_avail_list_seed.json        # legacy hardcoded config (backup / migration)
```

Registered in `api_app.py` with `init_db()` on startup.  
`SpecialAvailList.ensure_indexes()` runs in `server_runner.py` on worker startup.

## Environment

Uses the same MongoDB connection as the rest of the project:

- `MONGO_URI` — connection string
- `MONGO_DB` — database name

Podio field IDs used by the pipeline remain in environment variables in `special_avails.py` (`PODIO_PROPERTIES_APP_ID`, etc.). Only wholesaler name / email / Podio ID mappings moved to MongoDB.

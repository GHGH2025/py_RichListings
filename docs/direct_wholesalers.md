# Direct Wholesalers (MongoDB + CRUD API)

Direct wholesaler contacts were previously stored in `direct_wholeseller.json` and loaded at import time by workers. They are now stored in MongoDB and exposed via a CRUD API. Workers read from the database at runtime.

## Why two email fields

Gmail sender addresses often differ from the wholesaler's contact email (e.g. Constant Contact / ccsend proxy senders):

| Field | Purpose | Example |
|-------|---------|---------|
| `sender_email` | Gmail **From** address used for lookup (JSON outer key) | `sguerrero-housingig.com@shared1.ccsend.com` |
| `email` | Agent contact email written into listings (JSON inner `email`) | `sguerrero@housingig.com` |

## MongoDB collection

**Collection:** `direct_wholesalers`

| Field | Type | Notes |
|-------|------|-------|
| `sender_email` | string | Required, unique, lowercase |
| `email` | string | Required, contact email |
| `name` | string | Required |
| `phone` | string | Optional |
| `updateFlagForPodio` | boolean | When `true`, Podio update + Gmail label logic applies |
| `created_at` | datetime | Auto-set |
| `updated_at` | datetime | Auto-set on changes |

**Model:** `models/direct_wholesaler.py`  
**Service:** `services/direct_wholesaler_service.py`

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

## Workers that consume this data

| File | Behavior |
|------|----------|
| `listingDetails.py` | Matches email sender → overrides `agent_name`, `agent_phone`, `agent_email` on parsed listings; sets `direct_wholeseller` flag |
| `forward_completed_sources.py` | Applies "AI Direct Wholesaler Finder" Gmail label when `updateFlagForPodio` is true |

Both call `get_wholesaler_map()` from the service layer, which returns:

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

## File layout

```
models/
  __init__.py              # existing models (moved from models.py)
  direct_wholesaler.py     # DirectWholesaler document

routes/
  direct_wholesaler.py     # FastAPI CRUD routes

services/
  direct_wholesaler_service.py  # DB access, cache, JSON import

scripts/
  seed_direct_wholesalers.py    # CLI seed script

direct_wholeseller.json    # legacy source (backup only)
```

Registered in `api_app.py` with `init_db()` on startup.

## Environment

Uses the same MongoDB connection as the rest of the project:

- `MONGO_URI` — connection string
- `MONGO_DB` — database name (via `mongo_helper.py`; URI may also include db name for MongoEngine)

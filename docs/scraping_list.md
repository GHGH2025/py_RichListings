# Scraping List (MongoDB + CRUD API)

Gmail sender allow/skip patterns were previously hardcoded in `gmail_hourly_multi.py` under each account's `allowed_senders` and `skip_senders` arrays. They are now stored in MongoDB and exposed via a CRUD API. The Gmail fetch worker reads from the database at runtime.

## How sender filtering works

After Gmail messages are fetched for a time window, each message's **From** header is checked against patterns for that account:

1. If any **allow** patterns exist, the sender must match at least one (email, domain, full From line, or display name).
2. If any **skip** patterns match, the message is rejected even if it passed the allow list.

Pattern matching supports wildcards (`*`, `?`) via `fnmatch`. Domain-style patterns like `@homeventureinvestments.com` match the sender's domain.

| `list_type` | Purpose |
|-------------|---------|
| `allow` | Sender must match one of these patterns (when the list is non-empty) |
| `skip` | Sender is blocked if any pattern matches |

Set `active: false` to disable a pattern without deleting it.

## MongoDB collection

**Collection:** `scraping_list`

| Field | Type | Notes |
|-------|------|-------|
| `account_label` | string | Required. Gmail account id (`acct1`, `acct2`, …) — matches `ACCOUNTS[].label` in `gmail_hourly_multi.py` |
| `sender_pattern` | string | Required. Email, domain (`@foo.com`), or wildcard pattern |
| `list_type` | string | `allow` or `skip` (default `allow`) |
| `active` | boolean | When `false`, pattern is ignored at fetch time |
| `created_at` | datetime | Auto-set |
| `updated_at` | datetime | Auto-set on changes |

**Unique index:** `(account_label, sender_pattern, list_type)`

**Model:** `models/scraping_list.py`  
**Service:** `services/scraping_list_service.py`

## API

Start the API:

```bash
./run-api.sh
```

Base URL: `http://localhost:8000`  
Router prefix: `/api`  
Tag: `scraping-list`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/scraping-list` | List entries. Optional query: `account_label`, `list_type`, `active` |
| GET | `/api/scraping-list/patterns/{account_label}` | Resolved `allow` / `skip` arrays for one account |
| GET | `/api/scraping-list/{id}` | Get by Mongo `_id` |
| POST | `/api/scraping-list` | Create (409 if same account + pattern + list_type exists) |
| PUT | `/api/scraping-list/{id}` | Full replace |
| PATCH | `/api/scraping-list/{id}` | Partial update |
| DELETE | `/api/scraping-list/{id}` | Delete |
| POST | `/api/scraping-list/import-seed` | Upsert all entries from `scraping_list_seed.json` |

### Create / update body

```json
{
  "account_label": "acct1",
  "sender_pattern": "david@theligongroup.com",
  "list_type": "allow",
  "active": true
}
```

`list_type` must be `allow` or `skip`. Patterns are trimmed on write (not lowercased — matching is case-insensitive at fetch time).

### Example responses

**List (filtered):**

```bash
curl "http://localhost:8000/api/scraping-list?account_label=acct1&list_type=allow&active=true"
```

```json
[
  {
    "id": "...",
    "account_label": "acct1",
    "sender_pattern": "david@theligongroup.com",
    "list_type": "allow",
    "active": true,
    "created_at": "2026-06-09T15:29:56.048000",
    "updated_at": "2026-06-09T15:29:56.048000"
  }
]
```

**Patterns for an account:**

```bash
curl "http://localhost:8000/api/scraping-list/patterns/acct1"
```

```json
{
  "account_label": "acct1",
  "allow": ["david@theligongroup.com", "@homeventureinvestments.com", ...],
  "skip": []
}
```

**Import from seed JSON:**

```json
{
  "ok": true,
  "created": 95,
  "updated": 0,
  "skipped": 0,
  "total": 95
}
```

## Seeding / migration

One-time seed from the legacy hardcoded lists (saved in `scraping_list_seed.json`):

```bash
python scripts/seed_scraping_list.py
```

Or via API:

```bash
curl -X POST http://localhost:8000/api/scraping-list/import-seed
```

`scraping_list_seed.json` is kept as a backup and migration source. Re-running import upserts: existing rows are marked `active: true` and updated; new patterns are inserted.

## Workers that consume this data

| File | Behavior |
|------|----------|
| `gmail_hourly_multi.py` | Loads allow/skip patterns per account in `_ensure_paths()` via `get_patterns_for_account()` |
| `server_runner.py` | Runs `gmail_fetch_all()` every 5 minutes (calls `process_account` for each `ACCOUNTS` entry) |

`build_service_by_account()` in `gmail_hourly_multi.py` still uses the static `ACCOUNTS` list for OAuth paths only (`base_dir`, credentials/token files). Sender patterns always come from MongoDB.

The service caches patterns per account in-process for 60 seconds. Cache is cleared on any API create/update/delete/import.

## Account configuration (still in code)

OAuth and filesystem paths remain in `gmail_hourly_multi.py`:

```python
ACCOUNTS = [
    {
        "label": "acct1",
        "base_dir": os.path.join("accounts", "acct1"),
        "only_inbox": True,
        "fallback_lookback_min": 60,
        "credentials_filename": "credentials.json",
        "token_filename": "token.json",
        "state_filename": "state.json",
    },
    ...
]
```

To add a new Gmail account, add an entry here **and** seed allow/skip patterns in `scraping_list` for the same `label`.

## File layout

```
models/
  scraping_list.py           # ScrapingList document

routes/
  scraping_list.py           # FastAPI CRUD routes

services/
  scraping_list_service.py   # DB access, cache, JSON import

scripts/
  seed_scraping_list.py      # CLI seed script

scraping_list_seed.json      # legacy hardcoded senders (backup / migration source)
```

Registered in `api_app.py` with `init_db()` on startup.  
`ScrapingList.ensure_indexes()` runs in `server_runner.py` on worker startup.

## Environment

Uses the same MongoDB connection as the rest of the project:

- `MONGO_URI` — connection string
- `MONGO_DB` — database name (via `mongo_helper.py`; URI may also include db name for MongoEngine)

# Special Avails — Non-Active / Inactive Automation

Daily cron that finds **Active Podio properties** (created within the past **X** days) whose address **stops appearing** in that wholesaler’s avail email for **N consecutive days**, then POSTs a Podio catch webhook once.

This is separate from the existing special-avail snapshot/match flow (`special_avail` + match webhook). That flow compares yesterday’s emails to Active inventory once; this flow keeps a **miss streak** over multiple days.

## Behavior

1. Load active wholesalers from `special_avail_list` (sender emails + Podio item IDs).
2. For each wholesaler, fetch Active properties from the Podio Properties app.
3. Keep only properties with `created_on` within `SPECIAL_AVAIL_INACTIVE_LOOKBACK_DAYS` (default **7**).
4. Build today’s (America/New_York) address set from `parsed_listings` whose source email `from_info.email` is in that wholesaler’s `sender_emails`. Matching is **normalize-only** (no AI).
5. If the wholesaler sent **no email today**, miss streaks are **not** incremented (silent day skip).
6. Otherwise:
   - **Found** → reset `miss_streak` to 0
   - **Missed** → increment `miss_streak`
7. When `miss_streak >= SPECIAL_AVAIL_INACTIVE_MISS_DAYS` (default **3**) and webhook not yet fired → POST:

```json
{ "add": "<full property address>" }
```

to `SPECIAL_AVAIL_INACTIVE_WEBHOOK_URL`.

8. After a successful webhook → set WordPress `post_status` to `private` via `set_wp_post_status()` (same payload as `POST /public/wp/create`: `token`, `posttitle` = address, `post_status` = `private`). Tracked on the inactive tracker as `wp_private_ok` / `wp_private_at`.

## MongoDB

| Collection | Purpose |
|------------|---------|
| `special_avail_inactive_trackers` | Per Podio property streak / webhook state |
| `special_avail_inactive_job_runs` | Daily job I/O for metrics UI |

### Tracker statuses

| Status | Meaning |
|--------|---------|
| `watching` | Still tracking |
| `fired` | Inactive webhook sent once |
| `resolved` | Reappeared in email after a prior fire |

## Cron

Registered in `server_runner.py`:

```text
SPECIAL_AVAIL_INACTIVE_CHECK_TIME=22:00   # server local clock (same pattern as bounce check)
```

Default **22:00** so most of the EST calendar day’s emails are already ingested.

## Environment

| Variable | Default | Notes |
|----------|---------|-------|
| `SPECIAL_AVAIL_INACTIVE_LOOKBACK_DAYS` | `7` | X — Podio `created_on` window |
| `SPECIAL_AVAIL_INACTIVE_MISS_DAYS` | `3` | Consecutive miss threshold |
| `SPECIAL_AVAIL_INACTIVE_WEBHOOK_URL` | Podio catch URL | Body: `{ "add": "..." }` |
| `SPECIAL_AVAIL_INACTIVE_WEBHOOK_TIMEOUT` | `20` | Seconds |
| `SPECIAL_AVAIL_INACTIVE_CHECK_TIME` | `22:00` | Daily schedule (server local time) |

## Metrics UI

Sidebar: **Special Avails Non-Active** → `/metric/special-avails-inactive`

APIs:

- `GET /api/metrics/special-avail-inactive-jobs?summary=1`
- `GET /api/metrics/special-avail-inactive-trackers?status=watching|fired|resolved|all`

## Manual run

From `py_RichListings`:

```python
from special_avails.inactive_processor import run_special_avail_inactive_check
run_special_avail_inactive_check()           # normal
run_special_avail_inactive_check(force=True) # re-run same EST day
```

## Code

| File | Role |
|------|------|
| `special_avails/inactive_processor.py` | Job logic |
| `models/special_avail_inactive.py` | Mongo models |
| `server_runner.py` | Cron wiring |

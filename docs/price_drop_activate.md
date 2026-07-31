# 6% Price Drop — Activate (Podio Active + WP publish)

After the **30-day dedup** gate allows a listing because the price dropped **≥ 6%** vs a recent prior, a follow-up job marks the property **Active in Podio** (catch webhook) and sets WordPress `post_status` to **`publish`**.

This is separate from WordPress **REDUCED!!** updates (`wp_check_reduced`). See [dedup_30_day.md](./dedup_30_day.md) for the gate itself and [wordpress.md](./wordpress.md) for the REDUCED!! pipeline.

---

## Purpose

When the same property returns within 30 days at a meaningfully lower price:

1. Dedup stamps `price_drop_pass = true` (and related fields).
2. Activate cron:
   - Sets WordPress `post_status` to **`publish`** via addproperty `/create`
   - POSTs Podio Workflow Automation catch webhook `{ "add": "<address>" }` to mark **Active**
3. On both success → `price_drop_activated = true` (metrics Option A)

---

## File and schedule

| Item | Value |
|------|-------|
| **Module** | `pipeline/price_drop_activate.py` |
| **Function** | `process_price_drop_activations(limit=50)` |
| **Scheduler** | `server_runner.py` → `run_price_drop_activate` |
| **Interval** | Every **2 minutes** |
| **WP helper** | `integrations/wordpress/post_status.py` → `set_wp_post_status()` |
| **HTTP proxy** | `POST /public/wp/create` (same payload; uses the shared helper) |

---

## Candidate query

```text
price_drop_pass == true
AND price_drop_activated != true
```

Requires a usable address for `posttitle` / webhook `add`.

---

## Address used for WP + Podio

Prefer `geo_code_response.formatted_address` when present.

Otherwise build:

```text
{address}, {city}, {state} {zip}, USA
```

Example:

```text
1403 Ave J, Fort Pierce, FL 34950, USA
```

The same string is used as:

- WordPress `posttitle`
- Podio webhook body field `add`

---

## Step-by-step (per listing)

1. **WordPress public** — call shared helper:

```json
{
  "token": "<WP_API_TOKEN>",
  "posttitle": "<address string>",
  "post_status": "publish"
}
```

Underlying URL: `{WP_API_BASE}/create` (default inventory addproperty API).

2. **Podio Active webhook** — if WP succeeds:

```http
POST https://workflow-automation.podio.com/catch/2rtkutxl47po7x7
Content-Type: application/json

{ "add": "<address string>" }
```

2xx (`200` / `201` / `202`) = success. No cookies required.

3. **Both OK** → set:

| Field | Value |
|-------|--------|
| `price_drop_wp_public_at` | when WP succeeded |
| `price_drop_podio_webhook_at` | when webhook succeeded |
| `price_drop_activated` | `true` |
| `price_drop_activated_at` | now |
| `price_drop_activate_error` | cleared |

4. **Partial / full failure** → leave `price_drop_activated` false, set `price_drop_activate_error`, retry on next cron run.

---

## Fields stamped at dedup (≥6% pass only)

Set in `pipeline/dedup.py` when a prior exists and drop ≥ `PRICE_DROP_THRESHOLD` (0.06). **Not** set when there is no prior.

| Field | Meaning |
|-------|---------|
| `price_drop_pass` | Allowed specifically because of ≥6% drop |
| `price_drop_pct` | `(prev - curr) / prev` |
| `price_drop_prev_id` | Prior listing id (string) |
| `price_drop_prev_price` / `price_drop_curr_price` | Prices compared |
| `price_drop_activated` | Starts `false` until activate job succeeds |

---

## Environment

| Variable | Default | Notes |
|----------|---------|-------|
| `WP_API_TOKEN` | — | **Required** for WP public |
| `WP_API_BASE` | inventory addproperty base | `/create` appended |
| `PRICE_DROP_PODIO_ACTIVE_WEBHOOK_URL` | `https://workflow-automation.podio.com/catch/2rtkutxl47po7x7` | Podio catch |
| `PRICE_DROP_PODIO_ACTIVE_WEBHOOK_TIMEOUT` | `20` | Seconds |

---

## Metrics UI

Sidebar: **6% Price Drop** → `/metric/price-drop-6pct`

| Card / view | Mongo filter |
|-------------|--------------|
| **Activated** (success / Option A) | `price_drop_pass` + `price_drop_activated` |
| **Pending activate** | `price_drop_pass` and not activated |
| **All ≥6% passes** | `price_drop_pass` |
| **Blocked under 6%** | `rules_ai_reason` matches `/price not low enough/i` |

APIs (deals-details):

- `GET /api/metrics/price-drop-6pct/summary?period=...`
- `GET /api/metrics/price-drop-6pct/listings?view=activated|pending|blocked|all_pass&period=...`

The **Reduction** metrics page remains for WordPress `wp_check` / `REDUCED!!` only (it still shows a “Dedup blocked” count).

---

## How to verify

```javascript
// Passed ≥6% gate, waiting for activate
db.parsed_listings.find({ price_drop_pass: true, price_drop_activated: { $ne: true } }).limit(10)

// Fully activated (Option A)
db.parsed_listings.find({ price_drop_pass: true, price_drop_activated: true }).sort({ price_drop_activated_at: -1 }).limit(10)
```

Server logs:

```text
price_drop_activate: start
price_drop_activate: result={checked, activated, wp_ok, podio_ok, failed, skipped_no_addr}
```

Manual run from `py_RichListings`:

```python
from pipeline.price_drop_activate import process_price_drop_activations
print(process_price_drop_activations(limit=10))
```

---

## Common issues

| Symptom | Likely cause | What to check |
|---------|--------------|---------------|
| Stuck pending | WP token missing or title mismatch | `WP_API_TOKEN`, `price_drop_activate_error` |
| WP ok, Podio fails | Catch URL / automation error | `PRICE_DROP_PODIO_ACTIVE_WEBHOOK_URL`, Podio Flow history |
| Never stamped | No prior within 30d, or drop &lt; 6% | Dedup logs / `rules_ai_reason` |
| Address empty | No address / geo | `address`, `geo_code_response` |

---

## Key files

```
py_RichListings/
├── pipeline/dedup.py                      # Stamps price_drop_pass on ≥6%
├── pipeline/price_drop_activate.py        # WP public + Podio webhook
├── integrations/wordpress/post_status.py  # Shared create helper
├── routes/wordpress_proxy.py              # POST /public/wp/create
├── server_runner.py                       # Cron every 2 min
└── models/__init__.py                     # ParsedListing price_drop_* fields

deals-details/
├── app/metric/price-drop-6pct/page.tsx
├── components/metrics/PriceDrop6PctDashboard.tsx
└── app/api/metrics/price-drop-6pct/
```

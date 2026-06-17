# Podio Integration

This document explains how RichListings talks to **Podio** — what gets sent, when it runs, and how you can check that it is working.

Podio is your CRM. RichListings does **not** create new property listings in Podio from scratch. Instead, it:

- Links wholesaler emails to existing property records
- Saves buyer form submissions as new Podio items
- Matches buyers to properties
- Sends webhooks to Podio Workflow Automation when listings are posted or skipped

---

## Which Podio account and workspace?

All Podio API calls use the account set in `.env`:

| Setting | Purpose |
|---------|---------|
| `podioUsername` | The Podio login email (e.g. `Sell@floridahomebuyersllc.com`) |
| `podioPassword` | Password for that account |
| `PodioClientId` | OAuth app client ID |
| `PodioClientSecret` | OAuth app secret |
| `redirectUri` | OAuth redirect URL |

**The workspace is whatever workspace that Podio user belongs to.** There is no separate `PODIO_WORKSPACE_ID` in the code. To see the workspace name, log into Podio with that account and open any app listed below.

---

## Podio apps used

Each flow targets a specific **app** inside Podio, identified by an app ID:

| App | Env variable | Default app ID | What happens here |
|-----|--------------|----------------|-------------------|
| **Properties** | `PODIO_PROPERTIES_APP_ID` | `18339388` | Find property by address; update wholesaler link; buyer matching |
| **Wholesellers** | `PODIO_WHOLESELLERS_APP_ID` | `18339395` | Look up wholesaler by email |
| **Web Form Submissions / Buyers** | `PODIO_WEB_FORM_SUBMISSIONS_APP_ID` | `30585451` | New buyer preference form submissions |
| **Buyers** | `PODIO_BUYERS_APP_ID` | `30585451` | Buyer matching references on properties |

To confirm an app belongs to your workspace, open it in Podio or call:

```bash
curl -H "Authorization: Bearer <your_token>" \
  https://api.podio.com/app/18339388
```

The response includes the workspace name.

---

## The five Podio flows

### 1. Direct wholesaler linking

**What it does:** When a listing comes from a known direct wholesaler, RichListings finds the matching property in Podio and sets the **Wholeseller** field to the correct wholeseller record.

**Files:**
- `podio_direct_wholeseller.py` — search, match, and update logic
- `server_runner.py` — runs every **3 minutes** via `run_direct_wholeseller_linking`

**Step by step:**

1. A parsed listing has `direct_wholeseller = "not_processed"`.
2. The job reads the agent email from `complete_info.agent_email`.
3. It searches the **Properties** app by address and city.
4. It finds the wholeseller in the **Wholesellers** app by email.
5. It updates the property’s Wholeseller reference field in Podio.
6. MongoDB field `direct_wholeseller` is updated.

**Important:** The Podio update only happens if the wholesaler has `updateFlagForPodio: true` in the `direct_wholesalers` MongoDB collection. If it is `false`, the code skips the Podio write and only logs a message.

**MongoDB field:** `ParsedListing.direct_wholeseller`

| Value | Meaning |
|-------|---------|
| `not_processed` | Waiting to be picked up |
| `processed` | Successfully linked in Podio (or already correct) |
| `property_not_found` | No matching property in Podio |
| `wholeseller_not_found` | Property found, but no wholeseller with that email |
| `no_agent_email` | Listing has no agent email |
| `bypassed` | Sender is not a direct wholesaler |

**How to check:**
- Server logs: `run_direct_wholeseller_linking`, `Setting Wholeseller reference on property item ...`
- MongoDB: `db.parsed_listings.find({ direct_wholeseller: "processed" })`
- Podio UI: open a property and check the Wholeseller field

---

### 2. Buyer web form submissions

**What it does:** When someone fills out the buyer preferences form on the website, RichListings saves it in MongoDB and creates a new item in the **Web Form Submissions** Podio app.

**Files:**
- `podio_web_form_submissions.py` — creates and updates Podio items
- `buyer_submissions_api.py` — API endpoint that triggers the Podio create

**API:** `POST /api/buyer-submissions`

**Step by step:**

1. Frontend sends buyer contact info and property preferences.
2. Data is saved to MongoDB collection `web_form_buyer_submissions`.
3. `create_web_form_submission_item()` posts to Podio app `PODIO_WEB_FORM_SUBMISSIONS_APP_ID`.
4. On success, `podio_status` is set to `"sent"` and `podio_item_id` is stored.

**MongoDB collection:** `web_form_buyer_submissions`

| Field | Meaning |
|-------|---------|
| `podio_status` | `"not_sent"`, `"sent"`, or `"failed"` |
| `podio_item_id` | Podio item ID if created |
| `podio_error` | Error message if failed |

**How to check:**
- API response: `{ "podio_ok": true, "podio_item_id": 12345678 }`
- MongoDB: `db.web_form_buyer_submissions.find({ podio_status: "sent" })`
- Podio UI: new items in Web Form Submissions app

---

### 3. Listing posted / skipped webhooks

**What it does:** When a listing is posted to WhatsApp or skipped during selection, RichListings sends a JSON payload to Podio **Workflow Automation** catch URLs. This is not a direct Podio API item create — it triggers automations you set up in Podio.

**Files:**
- `ai_make_whatsapp_posts.py` — fires when listing is posted
- `post_selection.py` — fires when listing is skipped

**Env variables:**

| Variable | When it fires |
|----------|---------------|
| `POSTED_LISTING_WEBHOOK_URL` | Listing marked `status = "posted"` |
| `SKIPPED_LISTING_WEBHOOK_URL` | Listing skipped (bad region, quota, do-not-post city, etc.) |

**Posted webhook payload shape:**

```json
{
  "event": "listing_posted",
  "listing": { "...full listing data from MongoDB..." }
}
```

**How to check:**
- Podio Workflow Automation → open the catch flow → view run history
- MongoDB: `db.parsed_listings.find({ status: "posted" })` — these listings triggered the webhook
- Server logs on failure only: `[webhook] non-2xx` or `[webhook] failed`

---

### 4. Buyer matching

**What it does:** Matches parsed listings to buyers based on preferences. When a match is found, it updates the **Properties** app in Podio with references to matched buyer items.

**Files:**
- `buyer_matching_api.py` — matching logic and Podio updates
- `server_runner.py` — runs every `BUYER_MATCHING_CRON_MINUTES` (default **3 minutes**)

**Step by step:**

1. A listing gets `buyer_matching_status = "pending"` (often triggered by Globiflow with a Podio property item ID).
2. The cron job runs `process_pending_buyer_matching_batch()`.
3. It finds matching buyers from `web_form_buyer_submissions`.
4. It updates the property item in Podio with buyer references.
5. Status becomes `"matched"` or an error state.

**MongoDB fields on `ParsedListing`:**

| Field | Meaning |
|-------|---------|
| `buyer_matching_status` | `none`, `pending`, `processing`, `matched`, `errored_listing`, `skipped` |
| `buyer_matching_podio_item_id` | Podio property item ID |
| `buyer_matching_last_error` | Last error message if any |

**How to check:**
- Server logs: `run_buyer_matching_cron: result=...`
- MongoDB: `db.parsed_listings.find({ buyer_matching_status: "matched" })`
- Podio UI: property item should show linked buyer references

---

### 5. Special avails

**What it does:** Reads active properties from Podio for configured wholesalers, compares them to special-availability requests, and may call webhooks back to Podio.

**Files:**
- `special_avails.py`
- `services/special_avail_list_service.py` — wholesaler → Podio item ID mappings

**Env variables:**

| Variable | Purpose |
|----------|---------|
| `SPECIAL_AVAIL_MATCH_WEBHOOK_URL` | Webhook when a special avail match is found |
| `MANNY_MATCH_WEBHOOK_URL` | Webhook for Manny-specific matching |

**Runs:** every 3–5 minutes via `server_runner.py`

**How to check:**
- MongoDB `special_avails` collection for status fields
- Podio Workflow Automation history for webhook URLs

---

## Scheduled jobs (Podio-related)

| Job | Interval | File / function |
|-----|----------|-----------------|
| Direct wholesaler linking | Every 3 min | `run_direct_wholeseller_linking` → `process_direct_wholeseller_batch()` |
| Buyer matching | Every 3 min (configurable) | `run_buyer_matching_cron` → `process_pending_buyer_matching_batch()` |
| Special avails (active listings) | Every 3 min | `run_process_one_special_avail_with_active_listings()` |
| Special avails (matching) | Every 5 min | `run_process_one_special_avail_matching()` |

Buyer form submissions run **immediately** when the API is called — not on a schedule.

---

## Environment variables (summary)

| Variable | Required for |
|----------|--------------|
| `PodioClientId` | All Podio API calls |
| `PodioClientSecret` | All Podio API calls |
| `podioUsername` | All Podio API calls |
| `podioPassword` | All Podio API calls |
| `redirectUri` | OAuth token |
| `PODIO_PROPERTIES_APP_ID` | Properties app (default `18339388`) |
| `PODIO_WHOLESELLERS_APP_ID` | Wholesellers app (default `18339395`) |
| `PODIO_WEB_FORM_SUBMISSIONS_APP_ID` | Buyer form app (default `30585451`) |
| `PODIO_BUYERS_APP_ID` | Buyer matching (default `30585451`) |
| `PODIO_PROPERTIES_SPECIAL_PREFERENCES_FIELD_ID` | Manual special prefs on properties |
| `POSTED_LISTING_WEBHOOK_URL` | Posted listing webhook |
| `SKIPPED_LISTING_WEBHOOK_URL` | Skipped listing webhook |
| `SPECIAL_AVAIL_MATCH_WEBHOOK_URL` | Special avail webhook |
| `MANNY_MATCH_WEBHOOK_URL` | Manny match webhook |
| `IGNORE_PODIO_STATUS_FOR_TEST` | Test mode: ignore Active status filter |
| `BUYER_MATCHING_CRON_MINUTES` | How often buyer matching runs (default `3`) |

---

## How to verify Podio is working (quick checklist)

1. **Worker is running** — `GET http://<host>:8000/server-status`
2. **Auth works** — no `Podio auth error` in logs
3. **Direct wholesaler** — `direct_wholeseller: "processed"` in MongoDB
4. **Buyer form** — `podio_status: "sent"` in `web_form_buyer_submissions`
5. **Posted webhooks** — check Podio Workflow Automation run history
6. **Buyer matching** — `buyer_matching_status: "matched"` in MongoDB
7. **Podio UI** — open the Properties / Web Form Submissions apps and confirm recent changes

---

## Common problems

| Problem | Likely cause | What to do |
|---------|--------------|------------|
| `Podio auth error` in logs | Wrong username/password or expired OAuth app | Check `.env` credentials |
| Wholeseller not updating | `updateFlagForPodio` is `false` for that wholesaler | Set to `true` in `direct_wholesalers` collection |
| `property_not_found` | Address in email does not match any Active property in Podio | Check address spelling; verify property exists and is Active |
| `wholeseller_not_found` | Agent email not in Wholesellers app | Add wholeseller record in Podio |
| Buyer form `podio_status: "failed"` | Check `podio_error` field in MongoDB | Fix field mapping or auth |
| Webhook not firing | URL not set or listing never reached `posted` / `skipped` | Check env vars and listing `status` |
| Wrong workspace | Using credentials for a different Podio account | Confirm `podioUsername` matches the workspace you expect |

---

## Key files

```
py_RichListings/
├── podio_direct_wholeseller.py      # Wholesaler linking on Properties app
├── podio_web_form_submissions.py    # Buyer form → Podio create/update
├── buyer_submissions_api.py         # /api/buyer-submissions endpoint
├── buyer_matching_api.py            # Buyer ↔ property matching + Podio updates
├── special_avails.py                # Special avail reads + webhooks
├── ai_make_whatsapp_posts.py        # Posted listing webhook
├── post_selection.py                # Skipped listing webhook
└── server_runner.py                 # Scheduled jobs
```

For direct wholesaler configuration, see also: [direct_wholesalers.md](./direct_wholesalers.md)

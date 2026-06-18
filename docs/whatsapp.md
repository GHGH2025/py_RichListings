# WhatsApp Integration

This document explains how RichListings sends property listings to WhatsApp — from email inbox to message delivered.

WhatsApp sending uses **two programs working together**:

| Program | Location | Role |
|---------|----------|------|
| **Python worker** | `py_RichListings` | Reads listings, writes ad copy with AI, queues sends |
| **Node gateway** | `node_RichWhatsappListings` | Connects to WhatsApp (Baileys) and actually delivers messages |

Think of it like this: Python prepares the message and hands it off. Node is the phone that sends it.

---

## Big picture flow

```
Email arrives
    ↓
Parsed into a listing (MongoDB)
    ↓
Rules, images, verification
    ↓
AI writes WhatsApp ad copy
    ↓
Listing marked "posted", message queued
    ↓
Python calls Node gateway over HTTP
    ↓
Node sends via WhatsApp (DM or group)
```

---

## Step-by-step: from listing to WhatsApp message

### Step 1 — Email becomes a listing

- Gmail is checked every **5 minutes**.
- Emails from known senders are parsed into `ParsedListing` documents in MongoDB.

### Step 2 — Listing passes rules

- Duplicate check, AI rules, media verification run on a schedule.
- Good listings get `status = "passed"`.

### Step 3 — Selection and images

- `select_passed_listings_for_post()` picks listings to post (every **10 minutes**).
- Image curation and primary image check run (every **2 minutes**).
- Ready listings get `status = "ready_to_post"`.

### Step 4 — AI writes the WhatsApp post

**File:** `ai_make_whatsapp_posts.py`  
**Runs:** every **2 minutes**

1. Finds listings with `status = "ready_to_post"`.
2. Sends listing data + rules file (`ad_post_rules.txt`) to OpenAI.
3. Saves the result in `post_content`.
4. Updates the listing:
   - `status = "posted"`
   - `whatsapp_status = "pending"` (waiting to send)
   - `wp_status = "ready_to_process"` (starts WordPress pipeline)
5. Fires `POSTED_LISTING_WEBHOOK_URL` (Podio automation, if configured).

**Note:** This step does **not** send WhatsApp directly. It only writes the message and queues it.

### Step 5 — Message is sent

**File:** `whatsapp_sender.py`  
**Runs:** every **1 minute**

1. Finds listings where `whatsapp_status` is `"pending"` or `"failed"`.
2. Sets `whatsapp_status = "sending"`.
3. Calls the Node gateway (DM or group mode).
4. On success: `whatsapp_status = "sent"`.
5. On failure: `whatsapp_status = "failed"` (will retry on next run).

---

## DM mode vs group mode

You can send to **individual phone numbers** (DM) or to a **WhatsApp group**.

| Mode | Env setting | Sends to | Gateway endpoint |
|------|-------------|----------|------------------|
| **DM** | `WHATSAPP_SEND_MODE=dm` | `TEAM_WHATSAPP_NUMBERS` (comma-separated) | `POST /send` |
| **Group** | `WHATSAPP_SEND_MODE=group` | `WHATSAPP_GROUP_JIDS` (group IDs ending in `@g.us`) | `POST /group/send` |

### Switch mode at runtime

```http
POST http://<host>:8000/config/whatsapp-mode
Content-Type: application/json

{"mode": "dm"}
```

Use `"group"` for group mode. This saves to `.env` so it persists after restart.

### DM mode details

- Sends one message per phone number in `TEAM_WHATSAPP_NUMBERS`.
- Waits 2–4 seconds between each number (to avoid rate limits).
- Returns success if **at least one** number succeeds.

### Group mode details

- Sends one message to all groups listed in `WHATSAPP_GROUP_JIDS`.
- Group JIDs look like: `120363123456789012@g.us`

---

## Node gateway (node_RichWhatsappListings)

The Node app runs on port **3001** and uses **Baileys** to connect to WhatsApp Web.

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/send` | Send DM: `{ "to": "1234567890", "text": "...", "imageUrl": "..." }` |
| `POST` | `/group/send` | Send to groups: `{ "jids": ["...@g.us"], "text": "...", "imageUrl": "..." }` |
| `GET` | `/public/qr.png` | QR code to scan when WhatsApp session is not connected |

### First-time setup

1. Start the Node server.
2. Open `http://<host>:3001/public/qr.png` in a browser.
3. Scan the QR code with WhatsApp on your phone.
4. Session is saved in `node_RichWhatsappListings/auth/` — you should not need to scan again unless logged out.

### Connection webhooks

When the WhatsApp session connects or logs out, Node can notify an external URL:

- Env: `WHATSAPP_STATUS_WEBHOOK_URL`
- Events: `connected`, `logged_out`

---

## MongoDB fields (`parsed_listings`)

| Field | Values | Meaning |
|-------|--------|---------|
| `status` | `ready_to_post` → `posted` | Main listing lifecycle |
| `post_content` | text | The WhatsApp message (written by AI) |
| `whatsapp_status` | `pending` → `sending` → `sent` or `failed` | Send queue status |
| `images` | list of URLs | First HTTPS image is sent as `imageUrl` |
| `skipped_or_posted_at` | datetime | When listing was posted or skipped |
| `rules_ai_reason` | text | Error reason if AI post generation failed |

### Typical progression

```
status: ready_to_post
    ↓ (AI post job)
status: posted, whatsapp_status: pending, post_content: "🏠 3/2 in Miami..."
    ↓ (send queue job)
whatsapp_status: sending
    ↓
whatsapp_status: sent
```

---

## Scheduled jobs

From `server_runner.py`:

| Job | Interval | What it does |
|-----|----------|--------------|
| `run_select_passed_listings_for_post` | 10 min | Picks listings to post |
| `run_process_listings_ready_for_image_processing` | 2 min | Curates images |
| `run_process_primary_image_verification` | 2 min | Checks primary image |
| **`run_make_whatsapp_posts_from_ready_to_post`** | **2 min** | **AI writes post, marks posted** |
| **`run_process_whatsapp_queue`** | **1 min** | **Sends queued messages** |

Both Python worker and Node gateway must be running for messages to go out.

---

## Environment variables

### Python (`py_RichListings/.env`)

| Variable | Purpose |
|----------|---------|
| `WHATSAPP_SEND_MODE` | `dm` or `group` (default: `dm`) |
| `WHATSAPP_GATEWAY_URL_DM` | e.g. `http://localhost:3001/send` |
| `WHATSAPP_GATEWAY_URL_GROUP` | e.g. `http://localhost:3001/group/send` |
| `WHATSAPP_GATEWAY_TIMEOUT_SEC` | HTTP timeout (default: `20`) |
| `WHATSAPP_GATEWAY_AUTH_KEY` | Optional Bearer token for gateway |
| `TEAM_WHATSAPP_NUMBERS` | Comma-separated phone numbers for DM mode |
| `WHATSAPP_GROUP_JIDS` | JSON array or comma-separated group JIDs |
| `POSTED_LISTING_WEBHOOK_URL` | Webhook fired when listing is posted |
| `OPENAI_MODEL` | AI model for post generation |
| `STATUS_PORT` | FastAPI port (default: `8000`) |

### Node (`node_RichWhatsappListings/.env`)

| Variable | Purpose |
|----------|---------|
| `WHATSAPP_STATUS_WEBHOOK_URL` | Notified on connect / logout |
| `APP_BASE_URL` | Base URL (default: `http://localhost:3001`) |

---

## How to verify WhatsApp is working

### 1. Check the worker is running

```http
GET http://<host>:8000/server-status
```

Response includes `whatsapp_send_mode` (`dm` or `group`).

### 2. Check server logs

Look for these every 1–2 minutes:

- `make_whatsapp_posts_from_ready_to_post`
- `process_whatsapp_queue`

On send attempts you may see `DM>>` or `Group>>` with the URL and payload.

### 3. Check MongoDB

```javascript
// Recently posted, waiting to send
db.parsed_listings.find({ status: "posted", whatsapp_status: "pending" })

// Successfully sent
db.parsed_listings.find({ whatsapp_status: "sent" }).sort({ updated_at: -1 }).limit(10)

// Failed sends (will retry)
db.parsed_listings.find({ whatsapp_status: "failed" })
```

### 4. Check Node gateway is connected

- Visit `http://<host>:3001/public/qr.png` — if you see a QR code, WhatsApp is **not** connected yet.
- Or send a test message:

```http
POST http://<host>:3001/send
Content-Type: application/json

{ "to": "1234567890", "text": "test message" }
```

Expected response:

```json
{ "results": [{ "to": "1234567890", "status": "sent" }] }
```

If you get `503 not connected`, scan the QR code first.

### 5. Check your phone

In DM mode, messages appear in chats with each number in `TEAM_WHATSAPP_NUMBERS`.  
In group mode, check the configured WhatsApp group.

---

## Webhooks

| Webhook | When | Payload |
|---------|------|---------|
| `POSTED_LISTING_WEBHOOK_URL` | Listing marked posted | `{ "event": "listing_posted", "listing": {...} }` |
| `SKIPPED_LISTING_WEBHOOK_URL` | Listing skipped in selection | Skip reason + listing data |
| `WHATSAPP_STATUS_WEBHOOK_URL` | Node gateway connect/logout | `{ "event": "connected" \| "logged_out", ... }` |

Webhooks are best-effort — if they fail, the listing still posts; only a log line is written.

---

## Common problems

| Problem | Likely cause | Fix |
|---------|--------------|-----|
| `whatsapp_status` stuck on `pending` | Send queue job not running, or backlog > 5 | Check `server_runner.py` is running; wait for next cron |
| `whatsapp_status = failed` | Node gateway down, bad URL, empty `post_content` | Check Node on port 3001; verify env URLs |
| Node returns `503 not connected` | WhatsApp session disconnected | Scan QR at `/public/qr.png` |
| Listing stays `ready_to_post` | AI post generation failed | Check `rules_ai_reason` on the listing |
| No image in message | No HTTPS URL in `images` array | Ensure listing has a valid image URL |
| Image send fails | Image URL not publicly reachable | Use a public HTTPS link |
| Wrong recipients | `TEAM_WHATSAPP_NUMBERS` misconfigured | Fix `.env` |
| Group send does nothing | `WHATSAPP_GROUP_JIDS` empty or wrong | Verify group JID ends with `@g.us` |
| Mode mismatch | Testing DM endpoint but mode is `group` | Check `WHATSAPP_SEND_MODE` or `/config/whatsapp-mode` |
| `whatsapp_status` stuck on `sending` | Worker crashed mid-send | Manually set back to `pending` or `failed` in MongoDB |

---

## Twilio keepalive (optional, currently off)

`whatsapp_keepalive.py` can send template messages via Twilio to keep numbers active. The scheduled job in `server_runner.py` is **commented out**, so this does not run unless you enable it.

Env vars: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`, `TEAM_WHATSAPP_RECIPIENTS`

---

## Key files

```
py_RichListings/
├── server_runner.py           # Scheduler (WhatsApp jobs)
├── ai_make_whatsapp_posts.py  # AI post generation + posted webhook
├── whatsapp_sender.py         # Send queue + HTTP calls to Node
├── whatsapp_keepalive.py      # Twilio keepalive (optional)
├── config_runtime.py          # DM/group mode get/set
├── api_app.py                 # /server-status, /config/whatsapp-mode
├── post_selection.py          # Picks listings + skipped webhook
└── image_curation.py          # Image steps before ready_to_post

node_RichWhatsappListings/
├── server.js                  # Express on port 3001
├── services/whatsappService.js  # Baileys connection, QR, webhooks
└── routes/messageRoutes.js    # /send, /group/send
```

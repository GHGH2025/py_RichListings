# WhatsApp Inbound Listing Automation (User Guide)

This guide explains how to **receive listing posts from WhatsApp groups**, turn them into the same pipeline as email deals, and get them posted (including WhatsApp outbound ads).

Outbound sending (team ads) is covered in [whatsapp.md](./whatsapp.md). This doc is about **inbound** tracking.

---

## What this does

1. You pick a WhatsApp **group** and the **sellers** to listen to.
2. When those people post a listing (text and/or photo), Node saves the message.
3. Photos are uploaded to **S3** (public URL).
4. Python pulls pending messages into the normal listing pipeline (same as Gmail).
5. Listings go through media verify, rules, Dropbox gallery (if a Drive/Dropbox link was in the post), then outbound WhatsApp / Podio / WordPress as usual.

```
Seller posts in tracked WhatsApp group
        ↓
Node saves message (+ S3 image URL if photo)
        ↓
Python ingest → FilteredListingEmail (account: whatsapp)
        ↓
Same pipeline as email listings
        ↓
Outbound WhatsApp ad / Podio / WordPress
```

---

## Before you start

| Requirement | Why |
|-------------|-----|
| WhatsApp gateway connected | Scan QR at Node `/public/qr.png` if needed |
| Group Tracker UI available | Configure groups at `/whatsapp-groups` (Buyer Info frontend) |
| Python `py_RichListings` runner running | Job `run_whatsapp_ingest` every 1 minute |
| S3 configured on the **Node** server | So inbound photos are stored |
| Seller email in **Direct Wholesalers** (optional but recommended) | So Podio wholeseller mapping works |

### Node env (S3) — ops / server

On the WhatsApp Node host `.env`:

```env
LISTINGS_S3_BUCKET=your-bucket-name
LISTINGS_S3_PREFIX=images/whatsapp/
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

(Or use an EC2 IAM role instead of access keys.)  
Objects must be **publicly readable** so later WhatsApp outbound can attach the image.

Restart the Node WhatsApp service after changing env.

---

## Step 1 — Open Group Tracker

In the Buyer Info / configure frontend, go to:

**`/whatsapp-groups`**

Page title: **Group Tracker**.

You should see:

- Saved configs (left)
- Editor to pick a group and people (right)

If groups fail to load, the WhatsApp gateway is probably disconnected — reconnect / rescan QR first.

---

## Step 2 — Create or edit a track config

1. Click **New** (or open an existing config).
2. Select a **WhatsApp group**.
3. Check the people whose posts should become listings.
4. For each selected person, fill in:
   - **Display name** (optional)
   - **Seller email** (recommended) — must match `sender_email` in Direct Wholesalers
   - **Active** toggle
5. Leave **Tracking on** enabled for the group.
6. Click **Save config**.

Only messages from **selected + active** people in that group are saved. Everyone else is ignored.

### Seller email (important for Podio)

| Field | Purpose |
|-------|---------|
| Seller email on Group Tracker | Written onto each tracked message as `sender_email` |
| Same value in Direct Wholesalers → `sender_email` | Used to set agent contact + queue Podio wholeseller link |

If you skip the email:

- Listing can still process from WhatsApp text/images.
- Direct-wholesaler / Podio seller mapping usually **will not** match (lookup is email-based).

Also ensure that seller exists under **Direct Wholesalers** with the same `sender_email` (and a Podio Wholesellers email that matches the contact `email` field). See [direct_wholesalers.md](./direct_wholesalers.md).

---

## Step 3 — What gets saved from a WhatsApp post

| Seller posts… | What we store |
|---------------|---------------|
| Text only | Caption/body text |
| Photo + caption | Caption + image uploaded to S3 (`media_urls`) |
| Photo only (no caption) | S3 image only (still ingestable) |
| Google Drive / Dropbox gallery link in text | Link stays in the text; later pipeline can set `other_images_source` and build a Dropbox folder link |

Videos / documents are **not** uploaded to S3 yet (text/caption only if present).

---

## Step 4 — Automatic processing (no extra click)

After save, the system runs on a schedule:

| Job | Interval | Action |
|-----|----------|--------|
| WhatsApp ingest | ~1 min | `pending` messages → email-like rows (`account_label = whatsapp`) |
| Rest of pipeline | Existing crons | Parse → media → rules → post selection → WhatsApp ad → send |

Stale messages stuck in `processing` are reset about every **2 hours**.

You do **not** need to manually push messages into the pipeline once tracking is on.

---

## Step 5 — Gallery links (Drive → Dropbox)

If the WhatsApp post includes a **Google Drive folder** (or similar gallery) link in the text:

1. AI / media steps can copy it into `other_images_source` (same as email).
2. At **post selection**, the system may download that gallery and create `other_images_dropbox_link`.
3. That Dropbox link can appear in the final outbound WhatsApp caption (per ad rules).

This only works if:

- The link is extractable and accessible, and  
- The listing reaches post selection with `other_images_source` set.

The **photo attached** to the outbound WhatsApp message still comes from `images` (often the S3 URL from the inbound photo), not from Dropbox.

---

## How to verify it is working

### A. Config

- Group Tracker shows your group as **active**.
- People you care about are selected, **Active**, and have the correct **Seller email**.

### B. Message captured (Mongo / API)

Node API (example):

```http
GET http://<whatsapp-host>:3001/tracked-messages?limit=20
```

Expect recent docs with:

- `status`: `pending` → later `processed`
- `text` and/or `media_urls` (S3 `https://…`)
- `sender_email` when configured

### C. Pipeline

In Mongo / tools:

- `filtered_listing_emails` with `account_label: "whatsapp"`
- `parsed_listings` for that message progressing through statuses
- If seller email matched: `direct_wholeseller` moves toward `processed` after Podio link job

### D. Final post

- Listing reaches `posted` with `whatsapp_status` `pending` → `sent`
- Outbound message has caption + first image from `images` (S3 or other public URL)

---

## Common issues

| Symptom | Likely cause | What to do |
|---------|--------------|------------|
| No messages saved | Person not selected / inactive, or wrong group | Fix Group Tracker config; ensure participant JID is stored |
| Groups list empty / 503 | WhatsApp session disconnected | Rescan QR on Node gateway |
| Photos missing (`media_urls` empty) | S3 env missing or upload failed | Set `LISTINGS_S3_BUCKET` + AWS creds; check Node logs |
| Ingest errors `empty_text` | No caption and no S3 image | Fix S3; or ask seller to include caption |
| Seller not mapped in Podio | Email missing or not in Direct Wholesalers | Add matching `sender_email` on both sides |
| No Dropbox gallery in final ad | Drive link not extracted or upload failed | Check `other_images_source` / post-selection logs |
| Message stuck `processing` | Worker crash mid-job | Wait for stale reset (~2h) or set status back to `pending` |

---

## Quick checklist for a new wholesaler

1. Add them to **Direct Wholesalers** (`sender_email`, contact `email`, name, phone, Podio flag).
2. Open **Group Tracker** → select their group → select their WhatsApp number.
3. Enter the **same seller email** as `sender_email`.
4. Save; keep tracking **on**.
5. Ask them to post a test listing (photo + address text is ideal).
6. Confirm tracked message → WhatsApp ingest → listing appears and processes.

---

## Related docs

| Doc | Topic |
|-----|--------|
| [whatsapp.md](./whatsapp.md) | Outbound WhatsApp send (gateway, DM vs group) |
| [whatsapp_ad_generation.md](./whatsapp_ad_generation.md) | AI ad copy for outbound posts |
| [direct_wholesalers.md](./direct_wholesalers.md) | Seller email → Podio wholeseller mapping |
| [media_verify.md](./media_verify.md) | Images / gallery extraction |
| [post_selection.md](./post_selection.md) | Dropbox gallery upload stage |

---

## Technical pointers (for engineers)

| Piece | Location |
|-------|----------|
| Node track config + ingest persist | `node_RichWhatsappListings` |
| S3 upload helper | `node_RichWhatsappListings/utils/s3Upload.js` |
| Group Tracker UI | `react_RichBuyerInfoFrontEnd/WhatsAppGroupsConfig.tsx` → `/whatsapp-groups` |
| Python ingest | `ingestion/whatsapp.py` |
| Model | `models/whatsapp_tracked_messages.py` → collection `whatsapp_tracked_messages` |
| Scheduler | `server_runner.py` → `run_whatsapp_ingest`, `run_reset_stale_processing_whatsapp` |
